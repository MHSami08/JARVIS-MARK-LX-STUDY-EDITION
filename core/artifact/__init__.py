"""
core/artifact - the Artifact Studio: background design + safe rendering of study figures.

Nothing is imported here on purpose (startup time must not change). Import the submodules
you need lazily:  core.artifact.jobs, .sanitize, .design  (later: .plan, .compile, .lint, .store, .circuit)
"""
