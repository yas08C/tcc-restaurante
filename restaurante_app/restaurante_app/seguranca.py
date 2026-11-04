"""Pequenas defesas contra abuso: limite de requisições e QR code assinado."""
import hmac

from django.conf import settings
from django.core import signing
from django.core.cache import cache


def ip_do_cliente(request):
    """IP de quem fez a requisição. Em produção (atrás do proxy do Render) vale
    o último IP do X-Forwarded-For, que é o que o proxy acrescentou — o
    primeiro pode ser forjado pelo cliente."""
    encaminhado = request.META.get('HTTP_X_FORWARDED_FOR')
    if encaminhado and getattr(settings, 'EM_PRODUCAO', False):
        return encaminhado.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR', '')


def excedeu_limite(chave, limite, janela_segundos):
    """Conta uma tentativa para `chave` e diz se passou de `limite` na janela.
    Usa o cache do Django (por processo, a menos que você configure um cache
    compartilhado como Redis)."""
    k = f'limite:{chave}'
    if cache.add(k, 1, janela_segundos):
        return 1 > limite
    try:
        return cache.incr(k) > limite
    except ValueError:  # a chave expirou entre o add e o incr
        cache.set(k, 1, janela_segundos)
        return False


def limpar_limite(chave):
    cache.delete(f'limite:{chave}')


def _assinatura_mesa(restaurante_id, mesa):
    return signing.Signer(salt='qr-mesa').signature(f'{restaurante_id}:{mesa}')


def token_mesa(restaurante_id, mesa):
    """Token do QR code da mesa. Só quem tem o link/QR da mesa consegue pedir por ela."""
    return _assinatura_mesa(restaurante_id, mesa)


def token_mesa_valido(restaurante_id, mesa, token):
    if not token:
        return False
    return hmac.compare_digest(_assinatura_mesa(restaurante_id, mesa), str(token))
