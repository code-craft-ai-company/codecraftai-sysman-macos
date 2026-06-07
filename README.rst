=============================
Code Craft AI Sysman macOS
=============================

The ``codecraftai-sysman-macos`` package provides system-management primitives for macOS hosts, parallel to what ``codecraftai-sysman-linux-redhat`` provides for Red Hat Enterprise Linux. It implements the cross-OS contracts published by ``codecraftai-sysman-common`` over the standard macOS admin toolchain — Homebrew, ``launchctl``, ``networksetup``, the macOS application firewall (``pf``), ``defaults``, ``log show``, and friends — exposed through the same ``ICommandRunner`` / ``IFileOperator`` abstractions and ``Aspects``-style behaviour parameters used elsewhere in the suite.

Status
======

40 cross-OS features implemented (50 modules total). Test suite: 809/809 passing under ``testbase``. See ``specs/3-<feature>/`` for the per-OS requirements/design/tasks for each implemented feature.

Repository Structure
====================

- ``development/`` - Runtime environment scripts.
- ``repository-setup/`` - Repository homing and setup scripts.
- ``source/packages/ccai/sysman/macos/`` - Implementation modules.
- ``source/testroots/testbase/ccai/tests/sysman/macos/`` - testbase test suite.
- ``specs/`` - Feature specifications organised by phase and domain area.
- ``userguide/`` - Standards, conventions, and documentation.

Environment Setup
=================

When working in a Codex container:

.. code-block:: bash

   ./development/setup-agent-container

For local development:

.. code-block:: bash

   ./repository-setup/rehome-repository
   ./development/setup-environment

Running Tests
=============

Activate the virtual environment and run the testbase suite:

.. code-block:: bash

   source .venv/bin/activate
   testbase testing run --root source/testroots/testbase \
       --includes=testbase.ccai.tests.sysman.macos \
       --output=./.output

Standards and Conventions
=========================

- Naming conventions: ``userguide/10-02-naming-conventions.rst``
- Code organization: ``userguide/10-01-code-organization.rst``
- Coding standards: ``userguide/10-00-coding-standards.rst``

Additional Resources
====================

- User guide entry point: ``userguide/userguide.rst``
- Cross-OS contracts: ``../codecraftai-sysman-common/specs/contracts/``
