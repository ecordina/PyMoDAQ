"""What can go wrong with a Quantum Northwest temperature controller"""


class QnwError(Exception):
    """Base class of the errors of the controller and of its connection"""


class QnwConnectionError(QnwError):
    """The port cannot be opened or there is no controller behind it"""


class QnwTimeout(QnwError):
    """The controller did not answer a question in time"""


class QnwRejected(QnwError):
    """The controller did not understand a command (it answers with an error 9)"""


class QnwLimitError(QnwError, ValueError):
    """A value the controller (or its sample holder) cannot take"""
