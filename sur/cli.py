import dataclasses
import logging
import re
from typing import Mapping, Optional, Sequence, Iterator

import click
from click.core import Context, Parameter

from github3 import GitHub, login
from github3.exceptions import NotFoundError
from github3.repos.branch import Branch
from pygit2 import Signature

from .config import Config, GHRepository
from .update import run


def validate_github_token(ctx, param, token: str) -> GitHub:
    if not token.strip():
        raise click.BadParameter("No GitHub token provided.", ctx, param)
    try:
        github = login(token=token)
        logging.debug("logged in to user %r", github.me())
    except Exception as e:
        raise click.BadParameter(str(e), ctx, param)
    ctx.obj = github
    return github


GITHUB_REPOSITORY_RE = re.compile(
    r"^(?P<owner>[a-z\d]([a-z\d]|-(?=[a-z\d])){0,38})/"
    r"(?P<repository>[a-z\d]+((([._]|__|[-]*)[a-z\d]+)+))?$",
    re.IGNORECASE,
)


def validate_repository(ctx, param, repo: str) -> GHRepository:
    global GITHUB_REPOSITORY_RE
    ctx.ensure_object(GitHub)
    m = GITHUB_REPOSITORY_RE.match(repo)
    if not m:
        raise click.BadParameter(
            f"Invalid GitHub repository: {repo}", ctx, param
        )
    try:
        return ctx.obj.repository(**m.groupdict())
    except Exception as e:
        raise click.BadParameter(str(e), ctx, param)


def validate_ref(ctx, param, ref: str) -> str:
    if not ref.startswith(("refs/heads/", "refs/tags/")):
        raise click.BadParameter(
            f"Invalid ref: {ref}; must start with refs/heads/ or refs/tags/.",
            ctx,
            param,
        )
    return ref


SIGNATURE_RE = re.compile(
    r"^\s*(?P<name>[^<>]+)\s+<(?P<email>[^<>@]+@[^<>@]+)>\s*$",
    re.IGNORECASE,
)


def validate_signature(ctx, param, signature: str) -> Signature:
    m = SIGNATURE_RE.match(signature)
    if not m:
        raise click.BadParameter(
            f"Invalid signature: {signature}; must be in the form "
            f"`NAME <EMAIL>'.",
            ctx,
            param,
        )
    return Signature(
        name=m.group("name"),
        email=m.group("email"),
    )


def find_latest_branch(repo: GHRepository, branch_startswith: str) -> Branch:
    branches: Iterator[Branch] = repo.branches()
    branches = [branch for branch in branches if branch.name.startswith(branch_startswith)]

    branches.sort(key=lambda b: int(re.search(r'\d+$', b.name).group()), reverse=True)

    if not branches:
        raise NotFoundError(f"Not found startswith '{branch_startswith}' branch")

    return branches[0]


def acquire_valid_branches_for_repos(
    target: str,
    is_optional: bool,
    auto_latest_branch: bool,
    ctx: Context,
    param: Parameter,
    repo_dict: dict
) -> Optional[Branch]:
    try:
        repository = ctx.obj.repository(**repo_dict)
        
        if auto_latest_branch:
            branch = find_latest_branch(repository, target.replace("refs/heads/", ""))
            target = f"refs/heads/{branch.name}"
        else:
            branch_name = target or repository.default_branch
            branch = repository.branch(branch_name)

    except NotFoundError as e:
        if is_optional:
            logging.warning("%s: %s; skipping...", target, str(e))
            return None
        raise click.BadParameter(f"{target}: {e.message}", ctx, param)
    except Exception as e:
        raise click.BadParameter(str(e), ctx, param)

    return branch


def validate_and_acquire_branches(
    ctx: Context,
    param: Parameter,
    targets: Sequence[str]
) -> Mapping[GHRepository, Branch]:
    validated_branches = {}
    for target in targets:
        try:
            repo, branch = target.split(":", 1)
        except ValueError:
            raise click.BadParameter(f"No branch name: {target}")
        repo_dict = GITHUB_REPOSITORY_RE.match(repo).groupdict()
        
        if not repo_dict:
            raise click.BadParameter(f"Invalid GitHub repository: {target}", ctx, param)
        
        is_optional = branch and branch.endswith("?")
        auto_latest_branch = branch and branch.endswith("*")

        if auto_latest_branch:
            branch = branch.rstrip("*")
        if is_optional:
            branch = branch.rstrip("?")

        branch_obj = acquire_valid_branches_for_repos(branch, is_optional, auto_latest_branch, ctx, param, repo_dict)
        if branch_obj:
            validated_branches[ctx.obj.repository(**repo_dict)] = branch_obj

    return validated_branches

@click.command()
@click.option(
    "--github-token",
    "-t",
    "github",
    required=True,
    envvar="GITHUB_TOKEN",
    callback=validate_github_token,
    help="an access token to be used to fork, clone, push, and open PRs",
)
@click.option(
    "--source-repository",
    "-s",
    required=True,
    envvar="GITHUB_REPOSITORY",
    callback=validate_repository,
    help="the dependent repository to be referred as submodules by "
    "other repositories (e.g., org/repo-name)",
)
@click.option(
    "--ref",
    "-r",
    required=True,
    envvar="GITHUB_REF",
    callback=validate_ref,
    help="submodule heads in the dependent repositories will become to "
    "refer to to this (e.g., refs/tags/1.2.3, refs/heads/master)",
)
@click.option(
    "--committer",
    "-c",
    required=True,
    metavar="NAME <EMAIL>",
    callback=validate_signature,
    help="name and email address to be signed with on the commit "
    "(e.g., Your Name <email@example.com>)",
)
@click.option("--pr-title", "-T", metavar="FORMAT")
@click.option("--pr-description", "-D", metavar="FORMAT")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Do not actually open pull requests or push commits to target "
    "repositories/branches; however, it would still make new branches, forked "
    "repositories, and push commits",
)
@click.argument(
    "targets",
    metavar="TARGET_REPOSITORY:BRANCH",
    required=True,
    nargs=-1,
    callback=validate_and_acquire_branches,
)
@click.pass_context
def cli(
    ctx,
    github: GitHub,
    source_repository: GHRepository,
    ref: str,
    targets: Mapping[GHRepository, Branch],
    committer: Signature,
    pr_title: Optional[str] = None,
    pr_description: Optional[str] = None,
    dry_run: bool = False,
):
    """Update submodules in dependent repositories.  See also the README docs.

    TARGET_REPOSITORY is a GitHub repository (in the form of org/repo-name)
    that needs to update its submodule(s) referring the source repository.

    BRANCH is the branch name in the target repository that will be updated.
    A BRANCH with a trailing question mark, which is a mark for optional
    branches, is silently ignored iff it does not exist.

    """
    try:
        reference = source_repository.ref(ref[5:])
    except NotFoundError:
        raise click.BadParameter(
            f"No ref {ref} in the source repository {source_repository.full_name}",
            ctx,
        )
    assert github is not None
    config = Config(
        github=github,
        source_repository=source_repository,
        ref=reference,
        targets=targets,
        committer=committer,
        dry_run=dry_run,
    )
    if pr_title is not None:
        config = dataclasses.replace(config, pr_title_format=pr_title)
    if pr_description is not None:
        config = dataclasses.replace(
            config, pr_description_format=pr_description
        )
    logging.info("Configuration: %r", config)
    run(config)
