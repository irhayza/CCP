"""Stub minimal fastapi/starlette/pydantic agar handler HTTP di backend/main.py bisa dieksekusi tanpa FastAPI.
Hanya dipakai bila FastAPI asli tidak terpasang. Bukan pengganti TestClient: validasi skema sengaja tidak ditiru."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace


def real_fastapi_available() -> bool:
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        return not getattr(sys.modules.get("fastapi"), "__fake__", False)
    except ImportError:
        return False


def install() -> None:
    if real_fastapi_available() or getattr(sys.modules.get("fastapi"), "__fake__", False):
        return
    for name in [n for n in sys.modules if n.split(".")[0] in ("fastapi", "starlette", "pydantic")]:
        del sys.modules[name]

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    class Request:
        def __init__(self, headers=None, cookies=None, method="GET"):
            self.headers = {k.lower(): v for k, v in (headers or {}).items()}
            self.cookies, self.method = cookies or {}, method

    class Response:
        def __init__(self, content=None, media_type=None, headers=None, status_code=200, **kw):
            self.body, self.media_type, self.headers, self.status_code = content, media_type, headers or {}, status_code
            self.cookies, self.deleted = {}, []
            self.path, self.filename, self.background = kw.get("path"), kw.get("filename"), kw.get("background")

        def set_cookie(self, k, v, **kw):
            self.cookies[k] = (v, kw)

        def delete_cookie(self, k):
            self.deleted.append(k)

    class JSONResponse(Response):
        def __init__(self, content, status_code=200, **kw):
            super().__init__(content, "application/json", status_code=status_code)

    class FileResponse(Response):
        def __init__(self, path, media_type=None, filename=None, headers=None, background=None, **kw):
            super().__init__(None, media_type, headers)
            self.path, self.filename, self.background = path, filename, background

    class StreamingResponse(Response):
        def __init__(self, iterator, media_type=None, headers=None, **kw):
            super().__init__(None, media_type, headers)
            self.iterator = iterator

    class APIRouter:
        def __init__(self, prefix="", dependencies=None, **kw):
            self.prefix, self.dependencies, self.routes = prefix, dependencies or [], {}

        def _reg(self, method):
            def deco(path, **kw):
                def wrap(fn):
                    self.routes[(method, self.prefix + path)] = fn
                    return fn
                return wrap
            return deco

        def __getattr__(self, name):
            if name in ("get", "post", "delete", "put"):
                return self._reg(name.upper())
            raise AttributeError(name)

    class FastAPI(APIRouter):
        def __init__(self, **kw):
            super().__init__()
            self.state, self.handlers, self.mws, self.mounts, self.auth = SimpleNamespace(), {}, [], [], {}

        def exception_handler(self, exc):
            return lambda fn: self.handlers.setdefault(exc, fn) and fn

        def middleware(self, kind):
            def deco(fn):
                self.mws.append(fn)
                return fn
            return deco

        def include_router(self, router):
            for key, fn in router.routes.items():
                self.routes[key] = fn
                self.auth[key] = [d.dep for d in router.dependencies]

        def mount(self, path, app, name=None):
            self.mounts.append((path, app))

    class _Dep:
        def __init__(self, dep):
            self.dep = dep

    class _Info:
        def __init__(self, default=None, factory=None):
            self.default, self.factory = default, factory

    class BaseModel:
        def __init__(self, **kw):
            names = {}
            for c in reversed(type(self).__mro__):
                names.update(getattr(c, "__annotations__", {}))
            self._names = list(names)
            for n in names:
                if n in kw:
                    v = kw[n]
                else:
                    d = type(self).__dict__.get(n, getattr(type(self), n, None))
                    v = d.factory() if isinstance(d, _Info) and d.factory else d.default if isinstance(d, _Info) else d
                setattr(self, n, v)

        def model_dump(self):
            return {n: (v.model_dump() if isinstance(v := getattr(self, n), BaseModel) else v) for n in self._names}

    def Field(default=None, default_factory=None, **kw):
        return _Info(default, default_factory)

    fa = types.ModuleType("fastapi")
    fa.__fake__ = True
    fa.APIRouter, fa.FastAPI, fa.HTTPException, fa.Request, fa.Response = APIRouter, FastAPI, HTTPException, Request, Response
    fa.Depends = lambda fn: _Dep(fn)
    fa.Header = lambda default=None, **kw: default
    fa.Query = lambda default=None, **kw: default
    fr = types.ModuleType("fastapi.responses")
    fr.FileResponse, fr.JSONResponse, fr.StreamingResponse = FileResponse, JSONResponse, StreamingResponse
    fs = types.ModuleType("fastapi.staticfiles")
    fs.StaticFiles = lambda **kw: SimpleNamespace(**kw)
    st = types.ModuleType("starlette")
    sb = types.ModuleType("starlette.background")
    sb.BackgroundTask = lambda fn, *a, **k: SimpleNamespace(fn=fn, run=lambda: fn(*a, **k))
    pd = types.ModuleType("pydantic")
    pd.BaseModel, pd.Field = BaseModel, Field
    for m in (fa, fr, fs, st, sb, pd):
        sys.modules[m.__name__] = m
