from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .models import Produto, Notificacao

DIAS_AVISO_VALIDADE = 3
INTERVALO_VERIFICACAO_SEGUNDOS = 60


def fmt(q):
    """Decimal sem zeros à direita: 10.00 -> '10', 0.50 -> '0.5'."""
    return format(q.normalize(), 'f')


def _texto_validade(dias):
    if dias < 0:
        return f'venceu há {-dias} dia(s)'
    if dias == 0:
        return 'vence hoje'
    return f'vence em {dias} dia(s)'


def verificar_alertas_estoque(usuario, forcar=False):
    """Confere os produtos do usuário e cria/atualiza/resolve notificações de
    validade (já vencido ou vencendo em 3 dias), estoque baixo e estoque
    excedente. É chamada pelo banner de notificações, mas no máximo uma vez por
    minuto por usuário (a menos que `forcar`), para a consulta periódica do
    navegador não ficar escrevendo no banco a cada poucos segundos.
    Faz poucas queries no total (não uma por produto)."""
    if not forcar and not cache.add(f'alertas-estoque:{usuario.pk}', 1, INTERVALO_VERIFICACAO_SEGUNDOS):
        return

    hoje = timezone.localdate()
    limite_validade = hoje + timedelta(days=DIAS_AVISO_VALIDADE)

    abertas = {}
    for n in Notificacao.objects.filter(
        usuario=usuario, resolvida=False, produto__isnull=False, tipo__in=['validade', 'estoque', 'excesso']
    ):
        abertas.setdefault((n.tipo, n.produto_id), n)

    criar, atualizar, resolver = [], [], []

    def desejado(tipo, produto, mensagem):
        n = abertas.pop((tipo, produto.pk), None)
        if n is None:
            criar.append(Notificacao(usuario=usuario, tipo=tipo, produto=produto, mensagem=mensagem))
        elif n.mensagem != mensagem:
            n.mensagem = mensagem
            atualizar.append(n)

    for produto in Produto.objects.filter(usuario=usuario):
        # Só alerta validade se ainda há o que perder (vencido com estoque zerado não precisa de alerta).
        if produto.quantidade > 0 and produto.validade <= limite_validade:
            dias = (produto.validade - hoje).days
            desejado('validade', produto, f'{produto.nome} {_texto_validade(dias)}!')

        if produto.quantidade <= produto.quantidade_minima:
            desejado('estoque', produto,
                     f'{produto.nome} está com estoque baixo '
                     f'({fmt(produto.quantidade)} {produto.get_unidade_display()})!')

        if produto.estoque_excedente:
            desejado('excesso', produto,
                     f'{produto.nome} está com estoque acima do recomendado '
                     f'({fmt(produto.quantidade)} {produto.get_unidade_display()}, '
                     f'máximo {fmt(produto.quantidade_maxima)})!')

    resolver = [n.pk for n in abertas.values()]  # sobrou = o problema passou
    if criar:
        Notificacao.objects.bulk_create(criar)
    if atualizar:
        Notificacao.objects.bulk_update(atualizar, ['mensagem'])
    if resolver:
        Notificacao.objects.filter(pk__in=resolver).update(resolvida=True)
