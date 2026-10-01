"""ActionMesh core layers.

Each subpackage owns exactly one responsibility, and the dependency direction is
strictly downward::

    intent → graph → registry → permissions → executor → providers

C0 does not depend on anything above it. No layer may bypass another.
"""
