import logging
import subprocess
import tempfile
from typing import Optional, Tuple

from github3 import GitHub
from github3.exceptions import GitHubException
from github3.git import Commit, Reference
from github3.pulls import ShortPullRequest
from github3.repos.branch import Branch
from github3.session import BasicAuth, TokenAuth
from pygit2 import (
    GIT_FETCH_NO_PRUNE,
    GIT_RESET_HARD,
    GitError,
    Oid,
    RemoteCallbacks,
    Repository,
    Signature,
)
from pygit2.remote import Remote, TransferProgress

from .config import Config, GHRepository


def run(config: Config):
    for target_repo, target_branch in config.targets.items():
        update_target_repo(config, target_repo, target_branch)


def update_target_repo(
    config: Config, target_repo: GHRepository, target_branch: Branch
):
    source_repo = config.source_repository
    logging.info(
        f"Updating %s:%s...", target_repo.full_name, target_branch.name
    )
    cloned_repo = clone(target_repo, target_branch)
    updated = update_submodules(
        source_repo,
        config.ref,
        cloned_repo,
        config.committer,
    )
    if updated is None:
        logging.info(
            "Submodules in %s:%s are up to date.",
            target_repo.full_name,
            target_branch.name,
        )
        return
    commit, tree_changed = updated
    if not tree_changed:
        # Although submodule became to refer to a different commit,
        # if these commits have the same tree hash (i.e., identical
        # contents), it virtually affects nothing to the behavior.
        # So, such submodule updates don't have to be reviewed by
        # collaborators nor tested again.
        try:
            push_commit(
                config.github,
                target_repo,
                target_branch,
                cloned_repo,
                commit,
            )
        except GitError:
            # However, if the push fails (it's likely because the agent
            # is not authorized to push to the target repository), we
            # need to open a pull request anyway to ask for merging.
            logging.warning(
                "Failed to push commit %s to %s:%s; try to open a pull "
                "request instead...",
                commit.id.hex,
                target_repo.full_name,
                target_branch.name,
                exc_info=True,
            )
            pass
        else:
            # If the push succeeds, we don't need to open a pull request.
            try:
                source_repo.create_status(
                    config.ref.object.sha,
                    "success",
                    target_repo.commit(commit.id.hex).html_url,
                    f"Pushed a commit to {target_repo.full_name}:"
                    f"{target_branch.name}, which updates submodules "
                    f"referring to {source_repo.full_name}.",
                    f"submodule-updater/push/{target_repo.name}",
                )
            except GitHubException:
                logging.warning(
                    "Failed to create a status for %s@%s; try to give "
                    "appropriate permissions to the agent.",
                    source_repo.full_name,
                    config.ref.object.sha,
                )
            return
    # If submodule's tree data was changed, the commit which
    # updates the submodules' references should be reviewed through
    # a pull request.  It's also worth to run pull request checks.
    pr = open_pull_request(
        config.github,
        source_repo,
        config.ref,
        cloned_repo,
        target_repo,
        target_branch,
        commit,
        config.pr_title_format,
        config.pr_description_format,
    )
    try:
        source_repo.create_status(
            config.ref.object.sha,
            "success",
            pr.html_url,
            f"Created a pull request in {target_repo.full_name} to "
            f"update submodules referring to {source_repo.full_name}.",
            f"submodule-updater/pull/{target_repo.name}",
        )
    except GitHubException:
        logging.warning(
            "Failed to create a status for %s@%s; try to give "
            "appropriate permissions to the agent.",
            source_repo.full_name,
            config.ref.object.sha,
        )


def clone(
    target_repository: GHRepository,
    target_branch: Branch,
) -> Repository:
    d = tempfile.mkdtemp()
    logging.info(
        "Cloning %s:%s to %s...",
        target_repository.full_name,
        target_branch.name,
        d,
    )
    proc = subprocess.Popen(
        [
            "git",
            "clone",
            "--branch",
            target_branch.name,
            "--recurse-submodules",
            target_repository.clone_url,
            d,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with proc.stdout as stdout:
        for diag_msg in stdout:
            logging.debug("%s", diag_msg.rstrip())
    if proc.wait():
        raise GitError(
            f"Failed to clone {target_repository.full_name}:"
            f"{target_repository.name} to {d}",
        )
    repo = Repository(d)
    logging.info(
        "Cloned %s:%s to %s",
        target_repository.full_name,
        target_branch.name,
        d,
    )
    return repo


TreeChanged = bool


def update_submodules(
    submodule_source_repository: GHRepository,
    ref: Reference,
    cloned_repository: Repository,
    committer: Signature,
) -> Optional[Tuple[Commit, TreeChanged]]:
    ref_sha = ref.object.sha
    oid = Oid(hex=ref_sha)
    index = cloned_repository.index
    tree_changed = False
    count = 0
    for submodule_path in cloned_repository.listall_submodules():
        submodule = cloned_repository.lookup_submodule(submodule_path)
        if not match_remote_url(submodule_source_repository, submodule.url):
            continue
        subrepo: Repository = submodule.open()
        if subrepo.head.target == oid:
            continue
        try:
            ref_object = subrepo.revparse_single(ref_sha)
        except KeyError:
            logging.info(
                "%s is not found in %s; try to fetch...",
                ref_sha,
                subrepo.path,
                exc_info=True,
            )
            fetch_object(subrepo, oid)
            ref_object = subrepo.revparse_single(ref_sha)
        if ref.object.type == "tag":
            ref_object = ref_object.get_object()
        prev_tree_id = subrepo.revparse_single("HEAD").tree_id
        new_tree_id = ref_object.tree_id
        if prev_tree_id != new_tree_id:
            tree_changed = True
        subrepo.reset(oid, GIT_RESET_HARD)
        index.add(submodule_path)
        count += 1
    if not count:
        return None
    index.write()
    tree = index.write_tree(cloned_repository)
    message = f"""
Update {submodule_source_repository.name} submodule{
    's' if count > 1 else ''} to {
    submodule_source_repository.full_name}@{oid.hex}

This commit was automatically generated by Submodule Updater.
""".strip()
    commit_id = cloned_repository.create_commit(
        cloned_repository.head.name,
        committer,
        committer,
        message,
        tree,
        [cloned_repository.head.target],
    )
    return cloned_repository.get(commit_id), tree_changed


def open_pull_request(
    github: GitHub,
    submodule_source_repository: GHRepository,
    submodule_ref: Reference,
    cloned_repository: Repository,
    target_repository: GHRepository,
    target_branch: Branch,
    commit: Commit,
    pr_title_format: str,
    pr_description_format: str,
) -> ShortPullRequest:
    fork = get_or_create_fork(github, target_repository)
    fork_push_url = get_authenticated_push_url(github, fork)
    remote = cloned_repository.remotes.create(
        f"fork-{fork.owner.login}", fork_push_url
    )
    ref_name, ref_type = trim_ref(submodule_ref)
    temp_branch_name = (
        f"submodule-update/"
        f"{submodule_source_repository.name}/{ref_name}--"
        f"{commit.short_id}"
    )
    cloned_repository.create_branch(temp_branch_name, commit, True)
    push(cloned_repository, remote, f"refs/heads/{temp_branch_name}")
    format_ctx = dict(
        submodule_repository=submodule_source_repository,
        submodule_ref=submodule_ref,
        submodule_ref_name=ref_name,
        submodule_ref_type=ref_type,
        submodule_commit=submodule_ref.object,
    )
    return target_repository.create_pull(
        pr_title_format.format(**format_ctx),
        target_branch.name,
        f"{fork.owner.login}:{temp_branch_name}",
        pr_description_format.format(**format_ctx),
        maintainer_can_modify=True,
    )


def push_commit(
    github: GitHub,
    target_repository: GHRepository,
    target_branch: Branch,
    cloned_repository: Repository,
    commit: Commit,
):
    temp_branch_name = (
        f"submodule-update/{target_branch.name}--{commit.short_id}"
    )
    cloned_repository.create_branch(temp_branch_name, commit, True)
    push_url = get_authenticated_push_url(github, target_repository)
    remote = cloned_repository.remotes.create(
        f"tmp-push--{commit.short_id}", push_url
    )
    push(cloned_repository, remote, f"refs/heads/{temp_branch_name}")


def get_authenticated_push_url(github: GitHub, repo: GHRepository) -> str:
    session = github.session
    assert session.has_auth(), "No GitHub credentials found"
    auth = session.auth
    if isinstance(auth, TokenAuth):
        cred = github.me().login, auth.token
    elif isinstance(auth, BasicAuth):
        cred = auth.username, auth.password
    else:
        raise NotImplementedError(
            f"Unknown authentication type: {type(auth).__qualname__}"
        )
    return f"https://{cred[0]}:{cred[1]}@github.com/{repo.full_name}.git"


def trim_ref(ref: Reference) -> Tuple[str, Optional[str]]:
    if ref.ref.startswith("refs/heads/"):
        return ref.ref[11:], "branch"
    elif ref.ref.startswith("refs/tags/"):
        return ref.ref[10:], "tag"
    return ref.ref, None


def get_or_create_fork(
    github: GitHub, repository: GHRepository
) -> GHRepository:
    login = github.me().login
    for f in repository.forks():
        if f.owner.login == login:
            return f
    if repository.session is not github.session:
        repository = github.repository(repository.owner.login, repository.name)
    return repository.create_fork()


def match_remote_url(repo: GHRepository, remote_url: str) -> bool:
    possible_clone_urls = getattr(repo, "_possible_clone_urls", None)
    if possible_clone_urls is None:
        possible_clone_urls = {
            repo.clone_url,
            repo.git_url,
            repo.ssh_url,
            repo.html_url,
        }
        possible_clone_urls.update(
            [url[:-4] for url in possible_clone_urls if url.endswith(".git")]
        )
        repo._possible_clone_urls = possible_clone_urls
    return remote_url in possible_clone_urls


def fetch_object(repo: Repository, oid: Oid):
    for remote in repo.remotes:
        logging.info("Fetching %s from %s...", oid, remote.url)
        proc = subprocess.Popen(
            ["git", "fetch", remote.name, oid.hex],
            cwd=repo.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with proc.stdout as stdout:
            for diag_msg in stdout:
                logging.debug("%s", diag_msg.rstrip())
        if proc.wait():
            raise GitError(
                f"Failed to fetch object {oid.hex} from {remote.url}"
            )


def push(repo: Repository, remote: Remote, refspec: str):
    assert refspec.startswith((f"refs/heads/", f"refs/tags/"))
    proc = subprocess.Popen(
        ["git", "push", remote.name, f"{refspec}:{refspec}"],
        cwd=repo.workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with proc.stdout as stdout:
        for diag_msg in stdout:
            logging.debug("%s", diag_msg.rstrip())
    if proc.wait():
        raise GitError(
            f"Failed to push {refspec} to {remote.url}"
        )
