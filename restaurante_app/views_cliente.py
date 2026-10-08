"""Interface do CLIENTE (pública, sem login) + área do restaurante para ver os pedidos.

- O cliente só consegue: ver o cardápio, adicionar/remover pratos do pedido e
  enviar. Nada aqui altera pratos, preços ou estoque.
- A mesa só é definida pelo QR code da mesa (link assinado ?mesa=N&t=...), então
  ninguém consegue pedir "em nome" de uma mesa que não escaneou.
- O pedido enviado fica "recebido": o estoque só é baixado quando o restaurante
  CONFIRMA o pedido. Assim, pedidos falsos não esvaziam o estoque.
- O restaurante é identificado por um código aleatório (slug), nunca pelo login.
- O carrinho fica na SESSÃO do navegador do cliente.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import servicos
from .models import Prato, Pedido, ItemPedido, MESA_CHOICES, perfil_de
from .seguranca import excedeu_limite, ip_do_cliente, token_mesa, token_mesa_valido

LIMITE_POR_PRATO = 20
LIMITE_ITENS_POR_PEDIDO = 60
MAX_PEDIDOS_PENDENTES_POR_MESA = 3
MAX_PEDIDOS_POR_IP = 5          # por restaurante...
JANELA_PEDIDOS_SEGUNDOS = 600   # ...a cada 10 minutos


# ---------- helpers ----------

def _restaurante(slug):
    return get_object_or_404(User, perfil__slug_publico=slug, is_active=True)


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


def _voltar_ao_cardapio(slug, ancora=''):
    url = reverse('cliente_cardapio', args=[slug])
    return redirect(f'{url}#{ancora}' if ancora else url)


def link_da_mesa(request, restaurante, mesa):
    """URL completa (para o QR code) que libera pedidos na mesa informada."""
    slug = perfil_de(restaurante).slug_publico
    caminho = reverse('cliente_cardapio', args=[slug])
    return request.build_absolute_uri(f'{caminho}?mesa={mesa}&t={token_mesa(restaurante.pk, mesa)}')


# ---------- INTERFACE DO CLIENTE ----------

def cliente_cardapio(request, slug):
    restaurante = _restaurante(slug)

    # QR code da mesa: /cliente/<codigo>/?mesa=5&t=<assinatura>
    mesa_get = _mesa_valida(request.GET.get('mesa'))
    if mesa_get:
        if token_mesa_valido(restaurante.pk, mesa_get, request.GET.get('t')):
            request.session[_chave_mesa(restaurante)] = mesa_get
        else:
            messages.error(request, 'QR code da mesa inválido. Peça um novo ao atendente.')
        return _voltar_ao_cardapio(slug)

    mesa = request.session.get(_chave_mesa(restaurante))
    carrinho = _get_carrinho(request, restaurante)

    pratos = (
        Prato.objects.filter(usuario=restaurante, ativo=True)
        .prefetch_related('itens_ficha_tecnica__produto')
    )
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
        'nome_restaurante': perfil_de(restaurante).nome_exibicao,
        'slug': slug,
        'mesa': mesa,
        'pratos_info': pratos_info,
        'itens_pedido': itens_pedido,
        'total': total,
        'total_itens': sum(i['quantidade'] for i in itens_pedido),
    })


@require_POST
def cliente_adicionar(request, slug, pk):
    restaurante = _restaurante(slug)
    if not request.session.get(_chave_mesa(restaurante)):
        messages.error(request, 'Escaneie o QR code da sua mesa para fazer o pedido.')
        return _voltar_ao_cardapio(slug)

    prato = get_object_or_404(
        Prato.objects.prefetch_related('itens_ficha_tecnica__produto'),
        pk=pk, usuario=restaurante, ativo=True,
    )
    carrinho = _get_carrinho(request, restaurante)
    atual = carrinho.get(str(prato.pk), 0)

    if not prato.pode_ser_vendido():
        messages.error(request, f'"{prato.nome}" está indisponível no momento.')
    elif atual >= LIMITE_POR_PRATO:
        messages.error(request, f'Limite de {LIMITE_POR_PRATO} unidades por prato.')
    elif sum(carrinho.values()) >= LIMITE_ITENS_POR_PEDIDO:
        messages.error(request, f'Limite de {LIMITE_ITENS_POR_PEDIDO} itens por pedido.')
    else:
        carrinho[str(prato.pk)] = atual + 1
        _set_carrinho(request, restaurante, carrinho)
    return _voltar_ao_cardapio(slug, f'prato-{prato.pk}')


@require_POST
def cliente_remover(request, slug, pk):
    restaurante = _restaurante(slug)
    carrinho = _get_carrinho(request, restaurante)
    atual = carrinho.get(str(pk), 0)
    if atual > 1:
        carrinho[str(pk)] = atual - 1
    else:
        carrinho.pop(str(pk), None)
    _set_carrinho(request, restaurante, carrinho)
    return _voltar_ao_cardapio(slug, f'prato-{pk}')


@require_POST
def cliente_confirmar(request, slug):
    """Envia o pedido ao restaurante. NÃO mexe no estoque: isso só acontece
    quando o restaurante confirma o pedido na tela de Pedidos."""
    restaurante = _restaurante(slug)
    mesa = request.session.get(_chave_mesa(restaurante))
    carrinho = _get_carrinho(request, restaurante)

    if not mesa:
        messages.error(request, 'Escaneie o QR code da sua mesa antes de enviar o pedido.')
        return _voltar_ao_cardapio(slug)

    ids = [int(k) for k in carrinho if k.isdigit()]
    pratos = {
        str(p.pk): p
        for p in Prato.objects.filter(usuario=restaurante, ativo=True, pk__in=ids)
                              .prefetch_related('itens_ficha_tecnica__produto')
    }
    linhas = [(pratos[k], q) for k, q in carrinho.items() if k in pratos and q > 0]
    if not linhas:
        messages.error(request, 'Seu pedido está vazio. Adicione pelo menos um prato.')
        return _voltar_ao_cardapio(slug)

    indisponiveis = [p.nome for p, _ in linhas if not p.pode_ser_vendido()]
    if indisponiveis:
        messages.error(
            request,
            'Desculpe, parte do pedido ficou indisponível no momento. '
            'Remova algum prato ou chame o atendente.'
        )
        return _voltar_ao_cardapio(slug)

    # Freio contra spam: limite por IP e por mesa.
    if excedeu_limite(f'pedido:{restaurante.pk}:{ip_do_cliente(request)}', MAX_PEDIDOS_POR_IP, JANELA_PEDIDOS_SEGUNDOS):
        messages.error(request, 'Muitos pedidos em pouco tempo. Chame o atendente.')
        return _voltar_ao_cardapio(slug)
    pendentes = Pedido.objects.filter(usuario=restaurante, mesa=mesa, status='recebido').count()
    if pendentes >= MAX_PEDIDOS_PENDENTES_POR_MESA:
        messages.error(request, 'Sua mesa já tem pedidos aguardando o restaurante. Chame o atendente.')
        return _voltar_ao_cardapio(slug)

    with transaction.atomic():
        pedido = Pedido.objects.create(usuario=restaurante, mesa=mesa)
        for prato, quantidade in linhas:
            ItemPedido.objects.create(
                pedido=pedido,
                prato=prato,
                nome_prato=prato.nome,
                preco_unitario=prato.preco_venda,
                quantidade=quantidade,
            )

    _set_carrinho(request, restaurante, {})
    request.session[_chave_ultimo_pedido(restaurante)] = pedido.pk
    return redirect('cliente_pedido_confirmado', slug=slug, pedido_id=pedido.pk)


def cliente_pedido_confirmado(request, slug, pedido_id):
    restaurante = _restaurante(slug)
    # Só quem acabou de fazer o pedido (mesma sessão) consegue ver a tela
    if request.session.get(_chave_ultimo_pedido(restaurante)) != pedido_id:
        return _voltar_ao_cardapio(slug)
    pedido = get_object_or_404(Pedido, pk=pedido_id, usuario=restaurante)
    return render(request, 'cliente_pedido_confirmado.html', {
        'restaurante': restaurante,
        'nome_restaurante': perfil_de(restaurante).nome_exibicao,
        'slug': slug,
        'pedido': pedido,
        'itens': pedido.itens.all(),
    })


# ---------- ÁREA DO RESTAURANTE (exige login) ----------

@login_required
def pedido_list(request):
    pedidos = Pedido.objects.filter(usuario=request.user).prefetch_related('itens')[:100]
    slug = perfil_de(request.user).slug_publico
    link_cliente = request.build_absolute_uri(reverse('cliente_cardapio', args=[slug]))
    links_mesas = [
        (numero, link_da_mesa(request, request.user, numero)) for numero, _ in MESA_CHOICES
    ]
    return render(request, 'pedido_list.html', {
        'pedidos': pedidos,
        'link_cliente': link_cliente,
        'links_mesas': links_mesas,
    })


def _executar(request, funcao, pk, mensagem_ok):
    try:
        pedido = funcao(request.user, pk)
    except Pedido.DoesNotExist:
        messages.error(request, 'Pedido não encontrado.')
    except servicos.PedidoInvalido as e:
        messages.error(request, str(e))
    except servicos.EstoqueInsuficiente as e:
        messages.error(request, f'Estoque insuficiente para confirmar o pedido: {e}.')
    else:
        messages.success(request, mensagem_ok.format(pk=pedido.pk))
    return redirect('pedido_list')


@login_required
@require_POST
def pedido_confirmar(request, pk):
    return _executar(request, servicos.confirmar_pedido, pk, 'Pedido #{pk} confirmado e estoque baixado.')


@login_required
@require_POST
def pedido_cancelar(request, pk):
    return _executar(request, servicos.cancelar_pedido, pk, 'Pedido #{pk} cancelado (estoque devolvido, se já tinha sido baixado).')


@login_required
@require_POST
def pedido_entregar(request, pk):
    pedido = get_object_or_404(Pedido, pk=pk, usuario=request.user)
    if pedido.status != 'confirmado':
        messages.error(request, 'Confirme o pedido antes de marcá-lo como entregue.')
        return redirect('pedido_list')
    pedido.status = 'entregue'
    pedido.save(update_fields=['status'])
    messages.success(request, f'Pedido #{pedido.pk} marcado como entregue.')
    return redirect('pedido_list')
