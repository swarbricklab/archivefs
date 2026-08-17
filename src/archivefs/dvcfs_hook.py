"""Interpreter start-up hook: register archivefs' protocols with DVC.

Imported by the ``archivefs_dvc.pth`` that ``archivefs dvc enable`` installs, so it
runs in *every* interpreter in the environment. Two consequences shape it:

* **It must be cheap.** Nothing beyond ``sys``/``importlib`` is imported until DVC
  actually shows up, so ``python -c pass`` stays fast.
* **It must never raise.** A ``.pth`` that throws breaks every interpreter in the
  environment — including the one you would use to fix it.

DVC is not imported when the interpreter starts (the ``dvc`` console script imports
it afterwards), so this installs a one-shot ``sys.meta_path`` finder that waits for
``dvc.config_schema`` — the module whose import means DVC is about to resolve a URL
scheme — and registers immediately after it finishes loading. Deterministic: the
registration happens inside the import, not on a timer.
"""

from __future__ import annotations

import sys

#: Importing this module means DVC is being used and has not yet validated a URL.
TRIGGER = "dvc.config_schema"


class _RegisterAfter:
    """One-shot finder: load the trigger normally, then register."""

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname != TRIGGER:
            return None
        try:
            import importlib.util

            # Step aside so the real finders resolve the module, and stay aside:
            # this hook is needed exactly once.
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            spec = importlib.util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return None
            inner = spec.loader.exec_module

            def exec_module(module):
                inner(module)
                try:
                    from archivefs.dvcfs import register

                    register()
                except Exception:  # noqa: BLE001, S110 - never break a dvc run
                    pass

            spec.loader.exec_module = exec_module  # type: ignore[method-assign]
            return spec
        except Exception:  # noqa: BLE001 - as above, but for the finder itself
            return None


#: Set to "1" to stop the hook registering, without deleting the .pth. Useful for
#: reproducing "what does DVC do without archivefs", and for anyone who wants the
#: package installed but not wired into DVC.
OPT_OUT = "ARCHIVEFS_NO_DVC_HOOK"


def install() -> None:
    try:
        import os

        if os.environ.get(OPT_OUT) == "1":
            return
        if sys.modules.get(TRIGGER) is not None:
            # DVC was imported before us; register straight away.
            from archivefs.dvcfs import register

            register()
            return
        sys.meta_path.insert(0, _RegisterAfter())
    except Exception:  # noqa: BLE001, S110 - a raising .pth breaks python
        pass


install()
