"""Regras de negócio que mexem no estoque.

Toda alteração de quantidade passa por aqui, SEMPRE dentro de uma transação e
com o produto travado (select_for_update). Assim duas requisições simultâneas
não conseguem deixar o estoque negativo.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from .models import MovimentacaoEstoque, Pedido, Produto


class EstoqueInsuficiente(Exception):
    def __init__(self, nomes):
        self.nomes = list(nomes)
        super().__init__(', '.join(self.nomes))


class PedidoInvalido(Exception):
    """Pedido em um status que não permite a operação pedida."""


def _travar_produtos(ids):
    # order_by('pk') garante a mesma ordem de travamento em todas as requisições (evita deadlock)
    return {p.pk: p for p in Produto.objects.select_for_update().filter(pk__in=list(ids)).order_by('pk')}


def _registrar(usuario, produto, tipo, quantidade, observacao='', pedido=None):
    return MovimentacaoEstoque.objects.create(
        usuario=usuario, produto=produto, nome_produto=produto.nome, tipo=tipo,
        quantidade=quantidade, quantidade_resultante=produto.quantidade,
        observacao=observacao, pedido=pedido,
    )


@transaction.atomic
def baixar_estoque(usuario, necessidades, observacao, pedido=None):
    """Dá baixa em vários produtos de uma vez ({produto_id: quantidade}).
    Levanta EstoqueInsuficiente (sem alterar nada) se faltar qualquer insumo."""
    produtos = _travar_produtos(necessidades)
    faltando = [
        produtos[pk].nome if pk in produtos else 'produto removido'
        for pk, preciso in necessidades.items()
        if pk not in produtos or produtos[pk].quantidade < preciso
    ]
    if faltando:
        raise EstoqueInsuficiente(faltando)
    for pk, preciso in necessidades.items():
        produto = produtos[pk]
        produto.quantidade -= preciso
        produto.save(update_fields=['quantidade'])
        _registrar(usuario, produto, 'saida', preciso, observacao, pedido)


def necessidades_do_prato(prato, quantidade=1):
    necessidades = defaultdict(Decimal)
    for item in prato.itens_ficha_tecnica.all():
        necessidades[item.produto_id] += item.quantidade_usada * quantidade
    return necessidades


@transaction.atomic
def aplicar_movimentacao(usuario, produto_id, tipo, quantidade, observacao=''):
    """Saída, perda ou ajuste de contagem lançado manualmente."""
    produto = _travar_produtos([produto_id]).get(produto_id)
    if produto is None or produto.usuario_id != usuario.pk:
        raise PedidoInvalido('Produto não encontrado.')
    if tipo == 'ajuste':
        produto.quantidade = quantidade
    else:
        if quantidade > produto.quantidade:
            raise EstoqueInsuficiente([produto.nome])
        produto.quantidade -= quantidade
    produto.save(update_fields=['quantidade'])
    return _registrar(usuario, produto, tipo, quantidade, observacao)


@transaction.atomic
def registrar_compra(fornecedor):
    """Salva a compra e SOMA a quantidade comprada ao estoque do produto."""
    produto = _travar_produtos([fornecedor.produto_id])[fornecedor.produto_id]
    fornecedor.save()
    produto.quantidade += fornecedor.quantidade
    produto.save(update_fields=['quantidade'])
    _registrar(
        fornecedor.usuario, produto, 'entrada', fornecedor.quantidade,
        f'Compra de {fornecedor.quantidade} {produto.get_unidade_display()} com {fornecedor.nome_fornecedor}',
    )
    return fornecedor


@transaction.atomic
def vender_prato(usuario, prato):
    """Venda de balcão de 1 unidade: baixa o estoque e registra um pedido já
    entregue (para a receita entrar no Financeiro)."""
    necessidades = necessidades_do_prato(prato)
    if not necessidades:
        raise EstoqueInsuficiente([prato.nome])
    pedido = Pedido.objects.create(usuario=usuario, mesa=None, status='entregue')
    pedido.itens.create(prato=prato, nome_prato=prato.nome, preco_unitario=prato.preco_venda, quantidade=1)
    baixar_estoque(usuario, necessidades, f'Venda de 1x {prato.nome}', pedido)
    return pedido


@transaction.atomic
def confirmar_pedido(usuario, pedido_id):
    """O restaurante aceita o pedido do cliente: só agora o estoque é baixado."""
    pedido = Pedido.objects.select_for_update().get(pk=pedido_id, usuario=usuario)
    if pedido.status != 'recebido':
        raise PedidoInvalido('Só pedidos recebidos podem ser confirmados.')
    necessidades = defaultdict(Decimal)
    for item in pedido.itens.select_related('prato').prefetch_related('prato__itens_ficha_tecnica'):
        if item.prato is None:
            raise EstoqueInsuficiente([f'{item.nome_prato} (prato removido)'])
        for pk, qtd in necessidades_do_prato(item.prato, item.quantidade).items():
            necessidades[pk] += qtd
    baixar_estoque(usuario, necessidades, f'Pedido #{pedido.pk} ({pedido.mesa_display})', pedido)
    pedido.status = 'confirmado'
    pedido.save(update_fields=['status'])
    return pedido


@transaction.atomic
def cancelar_pedido(usuario, pedido_id):
    """Cancela o pedido. Se o estoque já tinha sido baixado, devolve tudo."""
    pedido = Pedido.objects.select_for_update().get(pk=pedido_id, usuario=usuario)
    if pedido.status not in ('recebido', 'confirmado'):
        raise PedidoInvalido('Este pedido não pode mais ser cancelado.')
    if pedido.status == 'confirmado':
        saidas = list(pedido.movimentacoes.filter(tipo='saida'))
        produtos = _travar_produtos([m.produto_id for m in saidas if m.produto_id])
        for mov in saidas:
            produto = produtos.get(mov.produto_id)
            if produto is None:  # produto excluído depois: não há onde devolver
                continue
            produto.quantidade += mov.quantidade
            produto.save(update_fields=['quantidade'])
            _registrar(usuario, produto, 'estorno', mov.quantidade,
                       f'Cancelamento do pedido #{pedido.pk}', pedido)
    pedido.status = 'cancelado'
    pedido.save(update_fields=['status'])
    return pedido
