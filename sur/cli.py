import logging
import re
from typing import Sequence, Union, Set

import click
from github3 import GitHub, login

from .config import Config, GHRepository


def validate_github_token(ctx, param, token: str) -> GitHub:
    try:
        github = login(token)
    except Exception as e:
        raise click.BadParameter(str(e))
    ctx.obj = github
    return github


GITHUB_REPOSITORY_RE = re.compile(
    r"^(?P<owner>[a-z\d]([a-z\d]|-(?=[a-z\d])){0,38})/"
    r"(?P<repository>[a-z\d]+((([._]|__|[-]*)[a-z\d]+)+))?$",
    re.IGNORECASE,
)


def validate_repository(
    ctx, param, repo: Union[str, Sequence[str]]
) -> Union[GHRepository, Set[GHRepository]]:
    global GITHUB_REPOSITORY_RE
    ctx.ensure_object(GitHub)
    if isinstance(repo, str):
        m = GITHUB_REPOSITORY_RE.match(repo)
        if not m:
            raise click.BadParameter(f"Invalid GitHub repository: {repo}")
        try:
            return ctx.obj.repository(**m.groupdict())
        except Exception as e:
            raise click.BadParameter(str(e))
    repos = set()
    for r in repo:
        m = GITHUB_REPOSITORY_RE.match(r)
        if not m:
            raise click.BadParameter(f"Invalid GitHub repository: {r}")
        try:
            repos.add(ctx.obj.repository(**m.groupdict()))
        except Exception as e:
            raise click.BadParameter(str(e))
    return repos


@click.command()
@click.option(
    "--github-token",
    "-t",
    "github",
    required=True,
    envvar="GITHUB_TOKEN",
    callback=validate_github_token,
)
@click.option(
    "--source-repository",
    "-s",
    required=True,
    envvar="GITHUB_REPOSITORY",
    callback=validate_repository,
)
@click.argument(
    "target-repository", required=True, nargs=-1, callback=validate_repository
)
def cli(
    github: GitHub,
    source_repository: GHRepository,
    target_repository: Set[GHRepository],
):
    config = Config(
        github=github,
        source_repository=source_repository,
        target_repositories=frozenset(target_repository),
    )
    logging.info("Configuration: %r", config)
