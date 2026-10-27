Public types
============

Requests reject unknown fields. Responses retain additional server fields.
Model IDs and advertised question-type names are extensible strings. Score
questions are not exposed by this SDK release.

.. automodule:: tazo.types
   :members: ChoiceQuestionParam, NoulQuestionParam, ChoiceQuestion, NoulQuestion, DecisionRequest, ChoiceAnswer, NoulAnswer, Usage, DecisionResponse, ModelCapabilities, Model, ModelList, HealthStatus, AudioFormat, SessionReady, SessionUpdated, DecisionEvent, SessionClosed, ErrorEvent, UnknownEvent
   :exclude-members: model_config, model_fields, model_computed_fields, nonblank_choices, boolean_criteria, nonblank_model, nonblank_questions, valid_distribution
