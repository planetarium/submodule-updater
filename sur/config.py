import dataclasses
from typing import AbstractSet, Union

from github3 import GitHub
from github3.repos import Repository, ShortRepository


GHRepository = Union[Repository, ShortRepository]


@dataclasses.dataclass(repr=True, frozen=True)
class Config:
    github: GitHub
    source_repository: GHRepository
    target_repositories: AbstractSet[GHRepository]
