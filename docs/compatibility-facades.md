# Compatibility Facade Retirement Policy

Krelcode keeps historical import paths only through the machine-readable
inventory in [`architecture/compatibility_facades.toml`](../architecture/compatibility_facades.toml).
The inventory is enforced by `tests/test_compatibility_facades.py` and by the
CI command below:

```bash
python scripts/check_compatibility_facades.py
```

## Required Records

Every compatibility Facade records:

- its file path and legacy import path;
- one or more replacement import paths;
- the first deprecated version;
- the planned removal version;
- whether production code still imports it;
- the exact production files that still use it.

`internal_uses` is a ratchet baseline. Moving an internal caller to the new path
must remove that caller from the list in the same change. Adding a new internal
import to a legacy path fails architecture tests.

## Facade Rules

1. A compatibility Facade may contain only a module docstring, imports,
   `__all__`, and the generic `__getattr__` / `__dir__` delegation used to keep
   wildcard exports stable.
2. It must not define classes, execute business logic, register runtime
   services, or call network, filesystem, or process APIs.
3. A Facade must point directly at a non-Facade module. Facade-to-Facade chains
   are rejected.
4. New production code must import from `new_imports`; only tests that verify
   backwards compatibility may continue using a legacy path.
5. The module docstring must begin with one of the recognized compatibility
   markers. This makes newly introduced Facades visible to the discovery check.

## Lifecycle

The current compatibility generation is deprecated in `0.81.0` and targets
removal in `1.0.0`. A Facade can be deleted only after all of these conditions
hold:

1. Its `internal_uses` list is empty.
2. Replacement-path import tests and documentation are in place.
3. The removal is called out in release notes.
4. The Facade file and its manifest record are deleted together.

At every major-version review, `reviewed_at` must be updated after checking all
removal conditions. If the current major version differs from `reviewed_at`,
the compatibility audit fails and blocks release until the inventory is
reviewed. A due Facade that still exists also fails the audit; postponement must
be an explicit reviewed change to the manifest, not an implicit exception.
