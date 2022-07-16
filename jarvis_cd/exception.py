class Error(BaseException):
    def __init__(self, error_code, child_error = None):
        self._error_code = error_code["id"]
        if child_error is None:
            self._error_message = error_code["msg"]
            print(self._error_message)
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
    #Specific error code
    CONFIG_REQUIRED = {"id": 1000, "msg": "Config is required. Check sample {}"}
    INVALID_SECTION = {"id": 1001, "msg": "Section {} is not recognized. Check sample {}"}
    INVALID_KEY = {"id": 1002, "msg": "Key {} is not recognized. Check sample {}"}
    INVALID_DEFAULT_CONFIG = {"id": 1003, "msg": "The module {} does not have a default configuration."}
    NOT_INSTALLED = {"id": 1004, "msg": "{} environment variable has not been set."}
    CONFIG_NOT_FOUND = {"id": 1005, "msg": "Config at {} does not exist."}
    HOSTFILE_NOT_FOUND = {"id": 1006, "msg": "Hostfile at {path} does not exist."}
    TOO_MANY_HOSTS_CHOSEN = {"id": 1007, "msg": "Hostfile at {path} does not contain {num_hosts} hosts, only {max_hosts}."}
    INVALID_TYPE = {"id": 1008, "msg": "{obj}: Has invalid type {t}."}
    INVALID_CMD_LIST = {"id": 1009, "msg": "ExecNode command list has a mix of both strings and nodes. {}"}
    #Orange FS error code
