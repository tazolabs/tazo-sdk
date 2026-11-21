Streaming sessions
==================

All questions share the session's audio scope and evaluation timing. Every
decision event contains one answer per question ID. A manual range commit
evaluates all questions together. Use separate sessions when settings differ.

.. automodule:: tazo.streaming
   :members: StreamingSession, AsyncStreamingSession
   :special-members: __next__, __anext__
