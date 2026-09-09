from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ZonaTemperatura, Notificacao, Fornecedor, MovimentacaoEstoque


@receiver(post_save, sender=Fornecedor)
def registrar_entrada_estoque(sender, instance, created, **kwargs):
    """Toda compra registrada (Fornecedor) gera automaticamente uma entrada
    no histórico de movimentações de estoque do produto comprado."""
    if not created:
        return
    produto = instance.produto
    MovimentacaoEstoque.objects.create(
        usuario=instance.usuario,
        produto=produto,
        tipo='entrada',
        quantidade=produto.quantidade,
        quantidade_resultante=produto.quantidade,
        observacao=f'Compra de {produto.quantidade} {produto.get_unidade_display()} '
                   f'com {instance.nome_fornecedor}',
    )


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