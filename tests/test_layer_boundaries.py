"""Architecture guards for the completed HTTP/application boundaries."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "apps/server/app"


def test_http_adapters_do_not_access_business_database():
    violations = []
    operations = {"query", "get", "add", "add_all", "flush", "commit", "rollback", "refresh", "delete", "execute"}
    for path in (APP / "api").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in {"db", "session"}
                    and node.func.attr in operations):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def test_application_and_capabilities_do_not_import_http_entrypoints():
    violations = []
    for folder in ("application", "services"):
        for path in (APP / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    ("fastapi", "starlette", "app.api", "app.main")
                ):
                    violations.append(f"{path.name}:{node.lineno}:{node.module}")
    assert violations == []


def test_business_routers_are_distinct_and_assembly_has_no_duplicate_endpoints():
    from app.api.v1 import agent, assets, auth, documents, exports, health, jobs, media, rewrite, routes
    routers = [module.router for module in (agent, assets, auth, documents, exports, health, jobs, media, rewrite)]
    assert len({id(router) for router in routers}) == len(routers)
    def iter_endpoints(router):
        for route in router.routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from iter_endpoints(included)
            else:
                yield from ((method, route.path) for method in route.methods)
    endpoints = list(iter_endpoints(routes.router))
    assert len(endpoints) == len(set(endpoints)) == 44
