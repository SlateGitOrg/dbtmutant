"""Project loader: compile a dbt project once and read the manifest."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path

import yaml

from .sqlutil import normalize_relations, parse, ref_names, render

DBT_ENV_VAR = "DBTMUTANT_DUCKDB_PATH"


class ProjectError(RuntimeError):
    """The dbt project could not be compiled or uses something unsupported."""


@dataclass(frozen=True)
class Seed:
    name: str
    path: Path
    column_types: dict[str, str]


@dataclass(frozen=True)
class Model:
    name: str
    unique_id: str
    file: str  # path relative to the project root
    raw_sql: str
    sql: str  # compiled, with relations normalised to ``schema.name`` and pretty-printed
    materialized: str
    depends_on: tuple[str, ...]  # names of the models and seeds this model reads


@dataclass(frozen=True)
class DataTest:
    name: str
    unique_id: str
    sql: str  # compiled SQL as dbt produced it
    depends_on: tuple[str, ...]
    attached_node: str | None
    severity: str  # "error" or "warn"
    error_if: str
    warn_if: str


@dataclass(frozen=True)
class GivenInput:
    kind: str  # "ref" or "source"
    name: str
    rows: tuple[dict, ...]


@dataclass(frozen=True)
class UnitTest:
    name: str
    unique_id: str
    model: str
    given: tuple[GivenInput, ...]
    expect_rows: tuple[dict, ...]


@dataclass
class Project:
    root: Path
    name: str
    models: dict[str, Model]
    seeds: dict[str, Seed]
    tests: list[DataTest]
    unit_tests: list[UnitTest]
    impact: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def relations(self) -> set[str]:
        return set(self.models) | set(self.seeds)

    def topo_order(self) -> list[str]:
        """Model names, parents first; ties broken alphabetically so runs are reproducible."""
        graph = {
            n: sorted(d for d in m.depends_on if d in self.models) for n, m in self.models.items()
        }
        sorter = TopologicalSorter(graph)
        sorter.prepare()
        order: list[str] = []
        while sorter.is_active():
            for name in sorted(sorter.get_ready()):
                order.append(name)
                sorter.done(name)
        return order

    def descendants(self, name: str) -> set[str]:
        """Models that (transitively) read ``name``, excluding ``name`` itself."""
        children: dict[str, set[str]] = {n: set() for n in self.models}
        for n, model in self.models.items():
            for parent in model.depends_on:
                if parent in children:
                    children[parent].add(n)
        seen: set[str] = set()
        stack = [name]
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    def tests_for(self, affected: set[str]) -> list[DataTest]:
        """Data tests that read any model in ``affected``."""
        return [t for t in self.tests if affected.intersection(t.depends_on)]

    @classmethod
    def load(cls, root: str | Path) -> Project:
        return load_project(root)


def _release_warehouse() -> None:
    """Close dbt-duckdb's process-wide connection so the warehouse file can be read afterwards.

    This reaches into an adapter internal (``DuckDBConnectionManager._ENV``). If a future adapter
    moves or renames it, dbtmutant carries on: only reading dbt's warehouse after a cross-check
    build is affected, and that read degrades to "no impact figures" instead of failing.
    """
    try:
        from dbt.adapters.duckdb.connections import DuckDBConnectionManager

        env = getattr(DuckDBConnectionManager, "_ENV", None)
        if env is not None:
            env.close()
            DuckDBConnectionManager._ENV = None
    except (ImportError, AttributeError):
        return


def run_dbt(args: list[str], project_dir: Path, work_dir: Path, db_path: Path) -> object:
    """Run a dbt command in-process, offline, with all output paths inside ``work_dir``."""
    from dbt.cli.main import dbtRunner

    env_backup = {k: os.environ.get(k) for k in (DBT_ENV_VAR, "DO_NOT_TRACK")}
    os.environ[DBT_ENV_VAR] = str(db_path)
    os.environ["DO_NOT_TRACK"] = "1"
    try:
        full = [
            *args,
            "--project-dir",
            str(project_dir),
            "--profiles-dir",
            str(project_dir),
            "--target-path",
            str(work_dir / "target"),
            "--log-path",
            str(work_dir / "logs"),
            "--no-version-check",
            "--quiet",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            result = dbtRunner().invoke(full)
        _release_warehouse()
        return result
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_project(root: str | Path) -> Project:
    """Compile the project with ``dbt compile`` and build a :class:`Project` from the manifest."""
    root = Path(root).resolve()
    if not (root / "dbt_project.yml").exists():
        raise ProjectError(f"{root} is not a dbt project (no dbt_project.yml)")
    with tempfile.TemporaryDirectory(prefix="dbtmutant-") as tmp:
        work = Path(tmp)
        result = run_dbt(["compile"], root, work, work / "compile.duckdb")
        if not getattr(result, "success", False):
            raise ProjectError(f"dbt compile failed: {getattr(result, 'exception', result)}")
        manifest = json.loads((work / "target" / "manifest.json").read_text())
    return project_from_manifest(root, manifest)


def project_from_manifest(root: Path, manifest: dict) -> Project:
    nodes = manifest["nodes"]
    names = {
        uid: node["name"]
        for uid, node in nodes.items()
        if node["resource_type"] in ("model", "seed")
    }
    seeds = {
        node["name"]: Seed(
            node["name"],
            root / node["original_file_path"],
            dict(node["config"].get("column_types") or {}),
        )
        for node in nodes.values()
        if node["resource_type"] == "seed"
    }
    known = set(names.values())

    models: dict[str, Model] = {}
    for uid, node in nodes.items():
        if node["resource_type"] != "model":
            continue
        materialized = node["config"].get("materialized", "view")
        if materialized == "ephemeral":
            raise ProjectError(f"model {node['name']} is ephemeral, which is not supported")
        tree = parse(node["compiled_code"])
        normalize_relations(tree, known)
        deps = tuple(sorted(names[d] for d in node["depends_on"]["nodes"] if d in names))
        models[node["name"]] = Model(
            name=node["name"],
            unique_id=uid,
            file=node["original_file_path"],
            raw_sql=node["raw_code"],
            sql=render(tree),
            materialized=materialized,
            depends_on=deps,
        )

    tests: list[DataTest] = []
    for uid, node in sorted(nodes.items()):
        if node["resource_type"] != "test":
            continue
        cfg = node["config"]
        attached = node.get("attached_node")
        tests.append(
            DataTest(
                name=node["name"],
                unique_id=uid,
                sql=node["compiled_code"],
                depends_on=tuple(
                    sorted(names[d] for d in node["depends_on"]["nodes"] if d in names)
                ),
                attached_node=names.get(attached) if attached else None,
                severity=str(cfg.get("severity", "error")).lower(),
                error_if=str(cfg.get("error_if", "!=0")),
                warn_if=str(cfg.get("warn_if", "!=0")),
            )
        )

    unit_tests: list[UnitTest] = []
    for uid, node in sorted(manifest.get("unit_tests", {}).items()):
        given = []
        for item in node["given"]:
            if item.get("format", "dict") != "dict":
                raise ProjectError(f"unit test {node['name']}: only dict fixtures are supported")
            parsed = ref_names(item["input"])
            if parsed is None:
                raise ProjectError(f"unit test {node['name']}: cannot read input {item['input']!r}")
            given.append(GivenInput(parsed[0], parsed[1], tuple(item.get("rows") or ())))
        expect = node["expect"]
        if expect.get("format", "dict") != "dict":
            raise ProjectError(f"unit test {node['name']}: only dict fixtures are supported")
        unit_tests.append(
            UnitTest(
                name=node["name"],
                unique_id=uid,
                model=node["model"],
                given=tuple(given),
                expect_rows=tuple(expect.get("rows") or ()),
            )
        )

    impact: dict[str, dict[str, str]] = {}
    config_file = root / "dbtmutant.yml"
    if config_file.exists():
        impact = (yaml.safe_load(config_file.read_text()) or {}).get("impact", {})

    return Project(
        root=root,
        name=manifest["metadata"].get("project_name") or root.name,
        models=models,
        seeds=seeds,
        tests=tests,
        unit_tests=unit_tests,
        impact=impact,
    )
