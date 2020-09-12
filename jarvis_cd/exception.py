class Error(BaseException):
    def __init__(self, error_code, child_error = None):
        self._error_code = error_code["id"]
        if child_error is None:
            self._error_message = "{}".format(error_code["msg"])
        else:
            self._error_message = str(child_error) + "\n{}".format(error_code["msg"])

    def format(self, *error_code_args):
        """
        Formatted the error message
        :param error_code_args:
        :return: Error
        """
        self._error_message = self._error_message.format(error_code_args)
        return self

    def __repr__(self):
        return {'error_code': self._error_code, 'error_message': self._error_message}

    def __str__(self):
        return self._error_message

class ErrorCode:
    """
    A class shows all the error code in Luxio
    """
    SUCCESS = {"id": 0, "msg": "SUCCESSFUL"}

    #General error code
    NOT_IMPLEMENTED = {"id": 1, "msg": "{} is not implemented"}
    #Specific error code
    CONFIG_REQUIRED = {"id": 1000, "msg": "Config is required. Check sample {}"}
    INVALID_SECTION = {"id": 1001, "msg": "Section {} is not recognized. Check sample {}"}
    INVALID_KEY = {"id": 1002, "msg": "Key {} is not recognized. Check sample {}"}

    #Orange FS error code
