class Error(BaseException):
    def __init__(self, error_code, child_error = None):
        self._error_code = error_code["id"]
        if child_error is None:
            self._error_message = error_code["msg"]
        else:
            self._error_message = str(child_error) + "\n{}".format(error_code["msg"])

    def format(self, *error_code_args):
        """
        Formatted the error message
        :param error_code_args:
        :return: Error
        """
        self._error_message = self._error_message.format(*error_code_args)
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
    LAUNCHER_NOT_FOUND = {"id": 2, "msg": "{} was not found"}

    #Hostfile errors
    HOSTFILE_NOT_FOUND = {"id": 1000, "msg": "Hostfile at {} does not exist."}
    TOO_MANY_HOSTS_CHOSEN = {"id": 1001,
                             "msg": "Hostfile at {} does not contain {} hosts, only {}."}
    INVALID_HOST_ID = {
        "id": 1002,
        "msg": "Hostfile numbering starts at 1 and ends at {}. Selected host {}."
    }
    INVALID_HOST_RANGE = {
        "id": 1003,
        "msg": "Hostfile numbering starts at 1 and ends at {}. Selected host in range {} - {}."
    }

    #Specific error code
    CONFIG_REQUIRED = {"id": 2000, "msg": "Config is required. Check sample {}"}
    INVALID_SECTION = {"id": 2001, "msg": "Section {} is not recognized. Check sample {}"}
    INVALID_KEY = {"id": 2002, "msg": "Key {} is not recognized. Check sample {}"}
    INVALID_DEFAULT_CONFIG = {"id": 2003, "msg": "The module {} does not have a default configuration."}
    NOT_INSTALLED = {"id": 2004, "msg": "{} environment variable has not been set."}
    CONFIG_NOT_FOUND = {"id": 2005, "msg": "Config at {} does not exist."}
    INVALID_TYPE = {"id": 2008, "msg": "{}: Has invalid type {}."}
    INVALID_CMD_LIST = {"id": 2009, "msg": "ExecNode command list has a mix of both strings and nodes. {}"}

    #SSH setup error codes
    NO_SSH_CONFIG = {"id": 2500, "msg": "Did not provide an SSH config YAML"}

    #Orange FS error code
