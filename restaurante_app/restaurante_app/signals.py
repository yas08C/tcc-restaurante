from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ZonaTemperatura, Notificacao, PerfilRestaurante


@receiver(post_save, sender=User)
def criar_perfil_restaurante(sender, instance, created, **kwargs):
    """Todo usuário ganha um perfil com o slug público usado nos links do cliente."""
    if created:
        PerfilRestaurante.objects.get_or_create(usuario=instance)


@receiver(post_save, sender=ZonaTemperatura)
def verificar_alerta_temperatura(sender, instance, **kwargs):
    """Dispara sempre que uma leitura de temperatura é salva (isto é, sempre
    que o ESP32 manda um dado novo pela view receber_temperatura).
    Cria uma Notificacao se a temperatura estiver fora da faixa segura, e
    resolve automaticamente quando ela voltar ao normal."""
    zona = instance

    if zona.em_alerta:
        ja_existe = Notificacao.objects.filter(
            usuario=zona.usuario, tipo='temperatura', zona=zona, resolvida=False
        ).exists()
        if not ja_existe:
            if zona.temp_atual > zona.temp_maxima:
                motivo = f'acima do máximo permitido ({zona.temp_maxima}°C)'
            else:
                motivo = f'abaixo do mínimo permitido ({zona.temp_minima}°C)'
            Notificacao.objects.create(
                usuario=zona.usuario,
                tipo='temperatura',
                zona=zona,
                mensagem=f'⚠️ {zona.get_zona_display()}: temperatura em {zona.temp_atual}°C, {motivo}!'
            )
    else:
        # Temperatura normalizou: resolve qualquer alerta antigo dessa zona.
        Notificacao.objects.filter(
            usuario=zona.usuario, tipo='temperatura', zona=zona, resolvida=False
        ).update(resolvida=True)
