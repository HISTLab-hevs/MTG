Models
======

The SQLAlchemy schema. Deletion behaviour lives here rather than in the call
sites: every relationship that mirrors a cascading foreign key is declared with
``passive_deletes``, so the database removes a subtree in one statement.

Every model is re-exported from the package, so callers import from
``app.models`` rather than from the module a class happens to live in.

.. automodule:: app.models
   :no-members:

Course
------

.. automodule:: app.models.course
   :members:

Documents
---------

.. automodule:: app.models.document
   :members:

Rules
-----

.. automodule:: app.models.rule
   :members:

Evaluations
-----------

.. automodule:: app.models.evaluation
   :members:

Analysis runs
-------------

.. automodule:: app.models.report
   :members:

Course reports
--------------

.. automodule:: app.models.course_report
   :members:

Users
-----

.. automodule:: app.models.user
   :members:
