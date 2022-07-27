import os

def GetPublicKey(key_dir, key_name):
    return os.path.join(key_dir, f"{key_name}.pub")

def GetPrivateKey(key_dir, key_name):
        return os.path.join(key_dir, key_name)