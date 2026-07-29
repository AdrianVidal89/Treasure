from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = settings.IA_FERNET_KEY
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def cifrar(texto_plano):
    return _fernet().encrypt(texto_plano.encode())


def descifrar(valor_cifrado):
    if isinstance(valor_cifrado, memoryview):
        valor_cifrado = bytes(valor_cifrado)
    try:
        return _fernet().decrypt(valor_cifrado).decode()
    except InvalidToken:
        return None
