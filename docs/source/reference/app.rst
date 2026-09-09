Application core
================

Configuration, the database session, authentication and the Jinja environment:
what every request passes through before it reaches a router.

Settings
--------

.. automodule:: app.config
   :members:

Database
--------

.. automodule:: app.db
   :members:
   :exclude-members: Base

.. autoclass:: app.db.Base
   :no-members:

   Declarative base every model inherits from. Its inherited members are
   SQLAlchemy's own and are documented upstream.

Dependencies
------------

.. automodule:: app.deps
   :members:

Security
--------

.. automodule:: app.security
   :members:

Templating
----------

.. automodule:: app.templating
   :members:

Application
-----------

.. automodule:: app.main
   :members:

Management CLI
--------------

.. automodule:: app.cli
   :members:

Rule provenance
---------------

.. automodule:: app.rule_sources
   :members:

Project information
-------------------

.. automodule:: app.project_info
   :members:
