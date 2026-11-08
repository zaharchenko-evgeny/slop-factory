"""Engine plugins (ADR-0026): one package per plugin, its `__init__.py` holding only its `PLUGIN` declaration.

The `factory_plugins` namespace keeps plugin names apart from installed packages; `plugins.KNOWN` lists them.
"""
