Tazo Python SDK
===============

This reference describes API v1's four public operations. See the
`Python SDK changelog <https://docs.tazolabs.com/sdk/python/changelog>`_
for releases and compatibility notes. Model capabilities remain discoverable
at runtime.

Multiple questions
------------------

Tazo Large supports up to ten choice and/or noul questions per upload or stream,
subject to deployment capabilities. Send a question-ID map; each response/event
returns the complete answer map. Questions share audio, state and text budget.
Check ``models.list()`` before use during regional rollouts. SDK 0.1.4 already
supports multiple questions; existing single-question calls remain compatible.
See the `multiple-question guide <https://docs.tazolabs.com/sdk/python/multiple-questions>`_
and ``examples/multiple_questions.py`` for sync/async uploads and streams.

.. toctree::
   :maxdepth: 2

   clients
   resources
   streaming
   types
   errors
