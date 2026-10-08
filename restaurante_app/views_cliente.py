"""Interface do CLIENTE (pública, sem login) + área do restaurante para ver os pedidos.

- O cliente só consegue: ver o cardápio, escolher a mesa, adicionar/remover pratos
  do pedido e confirmar. Nada aqui altera pratos, preços ou estoque do cardápio
  (o estoque só baixa quando o pedido é confirmado, igual à "Registrar Venda").
- O carrinho fica na SESSÃO do navegador do cliente (nada é salvo no banco
  até ele confirmar o pedido).
"""
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import (
    Prato, Produto, Pedido, ItemPedido, MovimentacaoEstoque, MESA_CHOICES,
)

LIMITE_POR_PRATO = 20


# ---------- helpers ----------

def _restaurante(username):
    return get_object_or_404(User, username=username, is_active=True)


def _chave_carrinho(restaurante):
    return f'carrinho_{restaurante.pk}'


def _chave_mesa(restaurante):
    return f'mesa_{restaurante.pk}'


def _chave_ultimo_pedido(restaurante):
    return f'ultimo_pedido_{restaurante.pk}'


def _get_carrinho(request, restaurante):
    return dict(request.session.get(_chave_carrinho(restaurante), {}))


def _set_carrinho(request, restaurante, carrinho):
    request.session[_chave_carrinho(restaurante)] = carrinho
    request.session.modified = True


def _mesa_valida(valor):
    try:
        mesa = int(valor)
    except (TypeError, ValueError):
        return None
    return mesa if mesa in dict(MESA_CHOICES) else None


def _voltar_ao_cardapio(username, ancora=''):
    url = reverse('cliente_cardapio', args=[username])
    return redirect(f'{url}#{ancora}' if ancora else url)


# ---------- INTERFACE DO CLIENTE ----------

def cliente_cardapio(request, username):
    restaurante = _restaurante(username)

    # QR code da mesa: /cliente/<restaurante>/?mesa=5 já deixa a mesa escolhida
    mesa_get = _mesa_valida(request.GET.get('mesa'))
    if mesa_get:
        request.session[_chave_mesa(restaurante)] = mesa_get
        return _voltar_ao_cardapio(username)
    if request.GET.get('trocar'):
        request.session.pop(_chave_mesa(restaurante), None)
        return _voltar_ao_cardapio(username)

    mesa = request.session.get(_chave_mesa(restaurante))
    carrinho = _get_carrinho(request, restaurante)

    pratos = Prato.objects.filter(usuario=restaurante, ativo=True)
    pratos_info = []
    itens_pedido = []
    total = Decimal('0')
    for prato in pratos:
        quantidade = carrinho.get(str(prato.pk), 0)
        pratos_info.append({
            'prato': prato,
            'quantidade': quantidade,
            'disponivel': prato.pode_ser_vendido(),
        })
        if quantidade > 0:
            subtotal = prato.preco_venda * quantidade
            total += subtotal
            itens_pedido.append({'prato': prato, 'quantidade': quantidade, 'subtotal': subtotal})

    return render(request, 'cliente_cardapio.html', {
        'restaurante': restaurante,
        'mesa': mesa,
        'mesas': MESA_CHOICES,
        'pratos_info': pratos_info,
        'itens_pedido': itens_pedido,
        'total': total,
        'total_itens': sum(i['quantidade'] for i in itens_pedido),
    })


@require_POST
def cliente_mesa(request, username):
    restaurante = _restaurante(username)
    mesa = _mesa_valida(request.POST.get('mesa'))
    if mesa:
        request.session[_chave_mesa(restaurante)] = mesa
    else:
        messages.error(request, 'Escolha uma mesa válida.')
    return _voltar_ao_cardapio(username)


@require_POST
def cliente_adicionar(request, username, pk):
    restaurante = _restaurante(username)
    prato = get_object_or_404(Prato, pk=pk, usuario=restaurante, ativo=True)
    carrinho = _get_carrinho(request, restaurante)
    atual = carrinho.get(str(prato.pk), 0)

    if not prato.pode_ser_vendido():
        messages.error(request, f'"{prato.nome}" está indisponível no momento.')
    elif atual >= LIMITE_POR_PRATO:
        messages.error(request, f'Limite de {LIMITE_POR_PRATO} unidades por prato.')
    else:
        carrinho[str(prato.pk)] = atual + 1
        _set_carrinho(request, restaurante, carrinho)
    return _voltar_ao_cardapio(username, f'prato-{prato.pk}')


@require_POST
def cliente_remover(request, username, pk):
    restaurante = _restaurante(username)
    carrinho = _get_carrinho(request, restaurante)
    atual = carrinho.get(str(pk), 0)
    if atual > 1:
        carrinho[str(pk)] = atual - 1
    else:
        carrinho.pop(str(pk), None)
    _set_carrinho(request, restaurante, carrinho)
    return _voltar_ao_cardapio(username, f'prato-{pk}')


@require_POST
def cliente_confirmar(request, username):
    restaurante = _restaurante(username)
    mesa = request.session.get(_chave_mesa(restaurante))
    carrinho = _get_carrinho(request, restaurante)

    if not mesa:
        messages.error(request, 'Escolha a sua mesa antes de confirmar o pedido.')
        return _voltar_ao_cardapio(username)

    ids = [int(k) for k in carrinho if k.isdigit()]
    pratos = {
        str(p.pk): p
        for p in Prato.objects.filter(usuario=restaurante, ativo=True, pk__in=ids)
                              .prefetch_related('itens_ficha_tecnica')
    }
    linhas = [(pratos[k], q) for k, q in carrinho.items() if k in pratos and q > 0]
    if not linhas:
        messages.error(request, 'Seu pedido está vazio. Adicione pelo menos um prato.')
        return _voltar_ao_cardapio(username)

    with transaction.atomic():
        # Quanto de cada insumo o pedido inteiro consome
        necessidades = defaultdict(int)
        for prato, quantidade in linhas:
            for item in prato.itens_ficha_tecnica.all():
                necessidades[item.produto_id] += item.quantidade_usada * quantidade

        produtos = {
            p.pk: p
            for p in Produto.objects.select_for_update().filter(pk__in=list(necessidades))
        }
        faltando = [
            produtos[pk].nome for pk, preciso in necessidades.items()
            if produtos[pk].quantidade < preciso
        ]
        if faltando:
            messages.error(
                request,
                'Desculpe, parte do pedido ficou indisponível no momento. '
                'Remova algum prato ou chame o atendente.'
            )
            return _voltar_ao_cardapio(username)

        pedido = Pedido.objects.create(usuario=restaurante, mesa=mesa)
        for prato, quantidade in linhas:
            ItemPedido.objects.create(
                pedido=pedido,
                prato=prato,
                nome_prato=prato.nome,
                preco_unitario=prato.preco_venda,
                quantidade=quantidade,
            )

        # Baixa o estoque (mesma regra do "Registrar Venda" do cardápio)
        for pk, preciso in necessidades.items():
            produto = produtos[pk]
            produto.quantidade -= preciso
            produto.save(update_fields=['quantidade'])
            MovimentacaoEstoque.objects.create(
                usuario=restaurante,
                produto=produto,
                tipo='saida',
                quantidade=preciso,
                quantidade_resultante=produto.quantidade,
                observacao=f'Pedido #{pedido.pk} (Mesa {mesa})',
            )

    _set_carrinho(request, restaurante, {})
    request.session[_chave_ultimo_pedido(restaurante)] = pedido.pk
    return redirect('cliente_pedido_confirmado', username=username, pedido_id=pedido.pk)


def cliente_pedido_confirmado(request, username, pedido_id):
    restaurante = _restaurante(username)
    # Só quem acabou de fazer o pedido (mesma sessão) consegue ver a tela
    if request.session.get(_chave_ultimo_pedido(restaurante)) != pedido_id:
        return _voltar_ao_cardapio(username)
    pedido = get_object_or_404(Pedido, pk=pedido_id, usuario=restaurante)
    return render(request, 'cliente_pedido_confirmado.html', {
        'restaurante': restaurante,
        'pedido': pedido,
        'itens': pedido.itens.all(),
    })


# ---------- ÁREA DO RESTAURANTE (exige login) ----------

@login_required
def pedido_list(request):
    pedidos = Pedido.objects.filter(usuario=request.user).prefetch_related('itens')[:100]
    link_cliente = request.build_absolute_uri(
        reverse('cliente_cardapio', args=[request.user.username])
    )
    return render(request, 'pedido_list.html', {
        'pedidos': pedidos,
        'link_cliente': link_cliente,
        'mesas': MESA_CHOICES,
    })


@login_required
@require_POST
def pedido_entregar(request, pk):
    pedido = get_object_or_404(Pedido, pk=pk, usuario=request.user)
    pedido.status = 'entregue'
    pedido.save(update_fields=['status'])
    messages.success(request, f'Pedido #{pedido.pk} marcado como entregue.')
    return redirect('pedido_list')
