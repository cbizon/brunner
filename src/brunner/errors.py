class BrunnerError(Exception):
    """Base class for expected brunner failures."""


class ConfigurationError(BrunnerError):
    """Invalid benchmark or runtime configuration."""


class ContractError(BrunnerError):
    """Invalid output contract or contract-bound submission."""


class ProviderError(BrunnerError):
    """Base class for provider execution failures."""


class ProviderRetryableError(ProviderError):
    """Provider failure that may succeed when retried."""


class ProviderTerminalError(ProviderError):
    """Provider failure that should not be retried."""


class BackendError(BrunnerError):
    """Base class for execution backend failures."""


class BackendConnectivityError(BackendError):
    """The backend cannot currently be contacted."""


class BackendRequestError(BackendError):
    """The backend rejected a request."""


class WorkloadFailure(BackendError):
    """A submitted workload terminated unsuccessfully."""


class ArtifactTransferError(BrunnerError):
    """Artifact collection was interrupted or incomplete."""


class IntegrityError(BrunnerError):
    """Recorded and observed artifact identities differ."""


class ChallengeMaterializationError(BrunnerError):
    """Challenge resource preparation failed before staging."""


class EvaluationError(BrunnerError):
    """Trusted evaluation failed."""


class AssessmentError(BrunnerError):
    """A trusted post-evaluation assessment failed."""


class ProviderSchemaError(AssessmentError):
    """A reviewer schema cannot be submitted to its provider."""
