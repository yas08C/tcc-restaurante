import json
import math
import os
import secrets
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.formats import number_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import servicos
from .forms import (
    ProdutoForm, ReservaForm, FornecedorForm, DespesaFixaForm,
    MovimentacaoEstoqueForm, PratoForm, ItemFichaTecnicaFormSet, CadastroForm,
)
from .models import (
    Produto, Reserva, Fornecedor, ZonaTemperatura, SensorESP32, DespesaFixa,
    Notificacao, MovimentacaoEstoque, Prato, ItemPedido,
    FAIXA_PADRAO_FREEZER, FAIXA_PADRAO_REFRIGERADO,
)
from .seguranca import excedeu_limite, ip_do_cliente, limpar_limite
from .utils import verificar_alertas_estoque


# ---------- AUTENTICAÇÃO ----------

def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            proximo = request.GET.get('next') or request.POST.get('next')
            if proximo and url_has_allowed_host_and_scheme(
                proximo, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(proximo)
            return redirect('home')
        messages.error(request, 'Usuário ou senha incorretos.')
    else:
        form = AuthenticationForm()
    return render(request, 'login.html', {'form': form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


def cadastro_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = CadastroForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    user = form.save()
            except IntegrityError:
                form.add_error('username', 'Já existe um usuário com esse nome.')
            else:
                # backend explícito: evita erro "multiple authentication backends"
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, f'Conta criada com sucesso! Bem-vindo(a), {user.username}.')
                return redirect('home')
        messages.error(request, 'Não foi possível criar a conta. Corrija os erros abaixo.')
    else:
        form = CadastroForm()
    return render(request, 'cadastro.html', {'form': form})


# ---------- HOME (interface inicial) ----------

@login_required
def home(request):
    return render(request, 'home.html')


# ---------- ESTOQUE / PRODUTOS (por usuário) ----------

@login_required
def produto_list(request):
    produtos = Produto.objects.filter(usuario=request.user).com_ultimo_preco()

    categoria_selecionada = request.GET.get('categoria', '')
    if categoria_selecionada:
        produtos = produtos.filter(categoria=categoria_selecionada)
    produtos = list(produtos)

    # Valor total do estoque parado, estimado pelo preço da última compra de
    # cada produto (produtos sem nenhuma compra registrada não entram na soma).
    valor_total_estoque = sum(
        (p.valor_em_estoque for p in produtos if p.valor_em_estoque is not None),
        start=Decimal('0'),
    )

    context = {
        'produtos': produtos,
        'categorias': Produto.CATEGORIA_CHOICES,
        'categoria_selecionada': categoria_selecionada,
        'valor_total_estoque': valor_total_estoque,
    }
    return render(request, 'produto_list.html', context)


# ---------- HISTÓRICO DE MOVIMENTAÇÕES DE ESTOQUE ----------

@login_required
def estoque_historico(request):
    movimentacoes = (
        MovimentacaoEstoque.objects.filter(usuario=request.user)
        .select_related('produto')[:500]
    )
    return render(request, 'estoque_historico.html', {'movimentacoes': movimentacoes})


@login_required
def movimentacao_create(request):
    if request.method == 'POST':
        form = MovimentacaoEstoqueForm(request.POST, usuario=request.user)
        if form.is_valid():
            try:
                servicos.aplicar_movimentacao(
                    request.user, form.cleaned_data['produto'].pk, form.cleaned_data['tipo'],
                    form.cleaned_data['quantidade'], form.cleaned_data['observacao'],
                )
            except servicos.EstoqueInsuficiente as e:
                # outra requisição consumiu o estoque entre a validação e o travamento
                form.add_error(None, f'Estoque insuficiente de {e}.')
            except servicos.PedidoInvalido:
                form.add_error('produto', 'Produto não encontrado.')
            else:
                messages.success(request, 'Movimentação de estoque registrada com sucesso.')
                return redirect('estoque_historico')
    else:
        form = MovimentacaoEstoqueForm(usuario=request.user)
    return render(request, 'movimentacao_form.html', {'form': form})


@login_required
def produto_create(request):
    if request.method == 'POST':
        form = ProdutoForm(request.POST, usuario=request.user)
        if form.is_valid():
            produto = form.save(commit=False)
            produto.usuario = request.user
            produto.save()
            messages.success(request, 'Produto cadastrado com sucesso.')
            return redirect('produto_list')
    else:
        form = ProdutoForm(usuario=request.user)
    return render(request, 'produto_form.html', {'form': form})


@login_required
def produto_update(request, pk):
    produto = get_object_or_404(Produto, pk=pk, usuario=request.user)
    if request.method == 'POST':
        form = ProdutoForm(request.POST, instance=produto, usuario=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Produto atualizado com sucesso.')
            return redirect('produto_list')
    else:
        form = ProdutoForm(instance=produto, usuario=request.user)
    return render(request, 'produto_form.html', {'form': form})


@login_required
def produto_delete(request, pk):
    produto = get_object_or_404(Produto, pk=pk, usuario=request.user)
    if request.method == 'POST':
        produto.delete()
        messages.success(request, 'Produto excluído com sucesso.')
        return redirect('produto_list')
    return render(request, 'produto_confirm_delete.html', {'produto': produto})


# ---------- FORNECEDORES (compras, por usuário) ----------

@login_required
def fornecedor_list(request):
    fornecedores = Fornecedor.objects.filter(usuario=request.user).select_related('produto')
    return render(request, 'fornecedor_list.html', {'fornecedores': fornecedores})


@login_required
def fornecedor_create(request):
    if request.method == 'POST':
        form = FornecedorForm(request.POST, usuario=request.user)
        if form.is_valid():
            fornecedor = form.save(commit=False)
            fornecedor.usuario = request.user
            servicos.registrar_compra(fornecedor)  # salva a compra E soma ao estoque
            messages.success(request, 'Compra registrada e quantidade somada ao estoque.')
            return redirect('fornecedor_list')
    else:
        form = FornecedorForm(usuario=request.user)
    return render(request, 'fornecedor_form.html', {'form': form})


@login_required
def fornecedor_update(request, pk):
    fornecedor = get_object_or_404(Fornecedor, pk=pk, usuario=request.user)
    if request.method == 'POST':
        form = FornecedorForm(request.POST, instance=fornecedor, usuario=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Registro atualizado com sucesso.')
            return redirect('fornecedor_list')
    else:
        form = FornecedorForm(instance=fornecedor, usuario=request.user)
    return render(request, 'fornecedor_form.html', {'form': form})


@login_required
def fornecedor_delete(request, pk):
    fornecedor = get_object_or_404(Fornecedor, pk=pk, usuario=request.user)
    if request.method == 'POST':
        fornecedor.delete()
        messages.success(request, 'Registro excluído com sucesso.')
        return redirect('fornecedor_list')
    return render(request, 'fornecedor_confirm_delete.html', {'fornecedor': fornecedor})


# ---------- CONTROLE DE AGENDAMENTO (visão geral das reservas) ----------

@login_required
def reserva_list(request):
    reservas = Reserva.objects.filter(usuario=request.user)
    return render(request, 'reserva_list.html', {'reservas': reservas})


@login_required
def reserva_delete(request, pk):
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)
    if request.method == 'POST':
        reserva.delete()
        messages.success(request, 'Reserva cancelada.')
        return redirect('reserva_list')
    return render(request, 'reserva_confirm_delete.html', {'reserva': reserva})


@login_required
def reserva_marcar_pago(request, pk):
    """Confirma o pagamento de uma reserva. Só reservas pagas entram no total do Financeiro."""
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)
    if request.method == 'POST':
        reserva.pago = True
        reserva.save(update_fields=['pago'])
        messages.success(
            request,
            f'Pagamento confirmado. Mesa {reserva.mesa} rendeu R$ {reserva.valor:.2f} '
            f'({reserva.quantidade_pessoas} pessoa(s) x R$ {reserva.VALOR_POR_PESSOA:.2f}) '
            f'e já foi somado ao Financeiro do mês.'
        )
    return redirect('reserva_list')

# ---------- RESERVA DE MESA (onde o cliente reserva) ----------

@login_required
def reserva_create(request):
    if request.method == 'POST':
        form = ReservaForm(request.POST, usuario=request.user)
        if form.is_valid():
            reserva = form.save(commit=False)
            reserva.usuario = request.user
            reserva.save()
            messages.success(request, 'Mesa reservada com sucesso!')
            return redirect('reserva_create')
    else:
        form = ReservaForm(usuario=request.user)
    return render(request, 'reserva_form.html', {'form': form})


# ---------- FINANCEIRO (resumo do mês) ----------

@login_required
def financeiro_view(request):
    hoje = timezone.localdate()

    resumo = defaultdict(lambda: {
        'total_gasto': Decimal('0'), 'aluguel': Decimal('0'), 'total_contas': Decimal('0'),
        'total_pessoas': 0, 'qtd_reservas_pagas': 0,
        'receita_pedidos': Decimal('0'), 'qtd_pedidos': 0,
    })

    # Os meses são calculados em Python, no fuso do restaurante (e não com
    # Extract no banco, que no MySQL exige as tabelas de fuso horário carregadas).

    # Compras de produtos (Fornecedor), agrupadas por mês/ano da compra
    for criado_em, total in Fornecedor.objects.filter(usuario=request.user).values_list('criado_em', 'preco_total'):
        dia = timezone.localtime(criado_em)
        resumo[(dia.year, dia.month)]['total_gasto'] += total

    # Despesas fixas (aluguel/água/energia/outros) já guardam ano/mes próprios
    for d in DespesaFixa.objects.filter(usuario=request.user):
        if d.tipo == 'aluguel':
            resumo[(d.ano, d.mes)]['aluguel'] += d.valor
        else:
            resumo[(d.ano, d.mes)]['total_contas'] += d.valor

    # Reservas pagas, agrupadas pelo mês/ano da DATA da reserva
    for data, pessoas in Reserva.objects.filter(usuario=request.user, pago=True).values_list('data', 'quantidade_pessoas'):
        resumo[(data.year, data.month)]['total_pessoas'] += pessoas
        resumo[(data.year, data.month)]['qtd_reservas_pagas'] += 1

    # Pedidos entregues (clientes nas mesas e vendas de balcão), pelo mês do pedido
    itens = ItemPedido.objects.filter(pedido__usuario=request.user, pedido__status='entregue')
    pedidos_contados = set()
    for pedido_id, criado_em, preco, qtd in itens.values_list(
            'pedido_id', 'pedido__criado_em', 'preco_unitario', 'quantidade'):
        dia = timezone.localtime(criado_em)
        mes = resumo[(dia.year, dia.month)]
        mes['receita_pedidos'] += preco * qtd
        if pedido_id not in pedidos_contados:
            pedidos_contados.add(pedido_id)
            mes['qtd_pedidos'] += 1

    nomes_meses = dict(DespesaFixa.MESES_CHOICES)
    resumo_mensal = []
    for (ano, mes), v in sorted(resumo.items(), key=lambda item: item[0], reverse=True):
        total_despesas_fixas = v['aluguel'] + v['total_contas']
        total_geral_despesas = v['total_gasto'] + total_despesas_fixas
        total_reservas = v['total_pessoas'] * Reserva.VALOR_POR_PESSOA
        total_receitas = total_reservas + v['receita_pedidos']
        saldo = total_receitas - total_geral_despesas
        resumo_mensal.append({
            'ano': ano,
            'mes': mes,
            'mes_nome': nomes_meses.get(mes, mes),
            'total_gasto': v['total_gasto'],
            'aluguel': v['aluguel'],
            'total_contas': v['total_contas'],
            'total_despesas_fixas': total_despesas_fixas,
            'total_geral_despesas': total_geral_despesas,
            'qtd_reservas_pagas': v['qtd_reservas_pagas'],
            'total_pessoas': v['total_pessoas'],
            'total_reservas': total_reservas,
            'receita_pedidos': v['receita_pedidos'],
            'qtd_pedidos': v['qtd_pedidos'],
            'total_receitas': total_receitas,
            'valor_por_pessoa': Reserva.VALOR_POR_PESSOA,
            'saldo': saldo,
            'is_mes_atual': (ano == hoje.year and mes == hoje.month),
        })

    # Dados do gráfico (cronológico), entregues ao template como JSON seguro.
    dados_grafico = [
        {'label': f"{m['mes_nome']}/{m['ano']}", 'despesas': float(m['total_geral_despesas']),
         'receita': float(m['total_receitas'])}
        for m in reversed(resumo_mensal)
    ]

    context = {
        'mes_referencia': hoje,
        'resumo_mensal': resumo_mensal,
        'dados_grafico': dados_grafico,
    }
    return render(request, 'financeiro.html', context)


# ---------- DESPESAS FIXAS (aluguel, água, energia, outros) ----------

@login_required
def despesa_list(request):
    despesas = DespesaFixa.objects.filter(usuario=request.user)

    # Resumo mensal com TODAS as despesas do restaurante: aluguel, água,
    # energia, outros (DespesaFixa) + compra de produtos (Fornecedor),
    # já somados e agrupados por mês/ano.
    resumo = defaultdict(lambda: {
        'aluguel': Decimal('0'), 'agua': Decimal('0'), 'energia': Decimal('0'),
        'outros': Decimal('0'), 'produtos': Decimal('0'),
    })

    for d in despesas:
        resumo[(d.ano, d.mes)][d.tipo] += d.valor

    for criado_em, total in Fornecedor.objects.filter(usuario=request.user).values_list('criado_em', 'preco_total'):
        dia = timezone.localtime(criado_em)
        resumo[(dia.year, dia.month)]['produtos'] += total

    nomes_meses = dict(DespesaFixa.MESES_CHOICES)
    resumo_mensal = []
    for (ano, mes), valores in sorted(resumo.items(), key=lambda item: item[0], reverse=True):
        total_mes = sum(valores.values())
        resumo_mensal.append({
            'ano': ano,
            'mes_nome': nomes_meses.get(mes, mes),
            'aluguel': valores['aluguel'],
            'agua': valores['agua'],
            'energia': valores['energia'],
            'outros': valores['outros'],
            'produtos': valores['produtos'],
            'total': total_mes,
        })

    return render(request, 'despesa_list.html', {'despesas': despesas, 'resumo_mensal': resumo_mensal})


@login_required
def despesa_create(request):
    if request.method == 'POST':
        form = DespesaFixaForm(request.POST, usuario=request.user)
        if form.is_valid():
            despesa = form.save(commit=False)
            despesa.usuario = request.user
            despesa.save()
            messages.success(request, 'Despesa cadastrada com sucesso.')
            return redirect('despesa_list')
    else:
        form = DespesaFixaForm(usuario=request.user, initial={'mes': timezone.localdate().month, 'ano': timezone.localdate().year})
    return render(request, 'despesa_form.html', {'form': form})


@login_required
def despesa_update(request, pk):
    despesa = get_object_or_404(DespesaFixa, pk=pk, usuario=request.user)
    if request.method == 'POST':
        form = DespesaFixaForm(request.POST, instance=despesa, usuario=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Despesa atualizada com sucesso.')
            return redirect('despesa_list')
    else:
        form = DespesaFixaForm(instance=despesa, usuario=request.user)
    return render(request, 'despesa_form.html', {'form': form})


@login_required
def despesa_delete(request, pk):
    despesa = get_object_or_404(DespesaFixa, pk=pk, usuario=request.user)
    if request.method == 'POST':
        despesa.delete()
        messages.success(request, 'Despesa excluída com sucesso.')
        return redirect('despesa_list')
    return render(request, 'despesa_confirm_delete.html', {'despesa': despesa})


# ---------- TEMPERATURA (ESP32) ----------

def _dados_zonas(usuario):
    return [
        {
            'zona': z.zona,
            'temp_maxima': number_format(z.temp_maxima, decimal_pos=2),
            'temp_minima': number_format(z.temp_minima, decimal_pos=2),
            'temp_atual': None if z.temp_atual is None else number_format(z.temp_atual, decimal_pos=2),
            'status': z.status_temperatura,
            'em_alerta': z.em_alerta,
        }
        for z in ZonaTemperatura.objects.filter(usuario=usuario).order_by('zona')
    ]


@login_required
def temperatura_view(request):
    zonas = ZonaTemperatura.objects.filter(usuario=request.user).order_by('zona')
    return render(request, 'restaurante/temperatura.html', {'zonas': zonas})


@login_required
def temperatura_dados(request):
    """JSON leve usado pela tela de temperatura para se atualizar sem recarregar a página."""
    return JsonResponse({'zonas': _dados_zonas(request.user)})


# Faixa de leitura fisicamente possível para DHT22 (-40..80) e DS18B20 (-55..125).
TEMP_MIN_VALIDA = Decimal('-55')
TEMP_MAX_VALIDA = Decimal('125')
LIMITE_ENVIOS_POR_MINUTO = 20  # o ESP32 envia a cada 30 s por zona


def _ler_temperatura(valor):
    """Converte o valor recebido em Decimal com 2 casas, ou None se for inválido
    (texto, NaN, infinito, booleano ou fora da faixa dos sensores)."""
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero):
        return None
    try:
        temperatura = Decimal(str(round(numero, 2)))
    except InvalidOperation:
        return None
    if not (TEMP_MIN_VALIDA <= temperatura <= TEMP_MAX_VALIDA):
        return None
    return temperatura


@csrf_exempt
@require_POST
def receber_temperatura(request):
    ip = ip_do_cliente(request)
    if excedeu_limite(f'esp32-ip:{ip}', LIMITE_ENVIOS_POR_MINUTO * 3, 60):
        return JsonResponse({'erro': 'muitas requisições'}, status=429)

    try:
        data = json.loads(request.body)
        token = data['token']
        zona = data['zona']
        valor = data['temperatura']
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
        return JsonResponse({'erro': 'payload inválido'}, status=400)

    if not isinstance(token, str) or not isinstance(zona, str):
        return JsonResponse({'erro': 'payload inválido'}, status=400)

    temperatura = _ler_temperatura(valor)
    if temperatura is None:
        return JsonResponse({'erro': 'temperatura inválida'}, status=400)

    if zona not in dict(ZonaTemperatura.ZONA_CHOICES):
        return JsonResponse({'erro': 'zona inválida'}, status=400)

    sensor = SensorESP32.objects.filter(token=token, ativo=True).select_related('usuario').first()
    if sensor is None:
        return JsonResponse({'erro': 'token inválido ou sensor inativo'}, status=401)

    if excedeu_limite(f'esp32-token:{sensor.pk}', LIMITE_ENVIOS_POR_MINUTO, 60):
        return JsonResponse({'erro': 'muitas requisições'}, status=429)

    # Se esse sensor foi travado numa zona específica (ex: DS18B20 -> Zona A),
    # recusa qualquer envio pra uma zona diferente. Evita que um token mande
    # dados pra zona errada e misture leituras entre zonas/usuários.
    if sensor.zona and sensor.zona != zona:
        return JsonResponse(
            {'erro': f'este token so pode enviar dados da zona {sensor.zona}'},
            status=403,
        )

    # Zona nova: faixa inicial conforme o tipo do sensor (freezer x refrigerado).
    minima, maxima = FAIXA_PADRAO_FREEZER if sensor.tipo_sensor == 'DS18B20' else FAIXA_PADRAO_REFRIGERADO
    zona_obj, _ = ZonaTemperatura.objects.get_or_create(
        usuario=sensor.usuario,
        zona=zona,
        defaults={'temp_minima': minima, 'temp_maxima': maxima},
    )
    zona_obj.temp_atual = temperatura
    zona_obj.save(update_fields=['temp_atual', 'atualizado_em'])
    # esse .save() dispara o signal verificar_alerta_temperatura automaticamente

    return JsonResponse({'status': 'ok', 'alerta': zona_obj.em_alerta})


# ---------- NOTIFICAÇÕES (banner de alertas) ----------

@login_required
def notificacoes_ativas(request):
    """Endpoint consultado via JavaScript para alimentar o banner de alertas.
    Também reaproveita a chamada para checar validade/estoque (no máximo uma
    vez por minuto por usuário), sem precisar de tarefa agendada."""
    verificar_alertas_estoque(request.user)

    notificacoes = Notificacao.objects.filter(usuario=request.user, resolvida=False)
    dados = [
        {'id': n.id, 'tipo': n.tipo, 'mensagem': n.mensagem}
        for n in notificacoes
    ]
    return JsonResponse({'notificacoes': dados})


# ---------- FICHA TÉCNICA (PRATOS DO CARDÁPIO) ----------

@login_required
def prato_list(request):
    pratos = Prato.objects.filter(usuario=request.user).prefetch_related('itens_ficha_tecnica__produto')
    pratos = list(pratos)
    pratos_info = [{'prato': p, 'pode_vender': p.pode_ser_vendido()} for p in pratos]
    return render(request, 'prato_list.html', {'pratos_info': pratos_info})


@login_required
def prato_create(request):
    if request.method == 'POST':
        form = PratoForm(request.POST)
        formset = ItemFichaTecnicaFormSet(request.POST, form_kwargs={'usuario': request.user})
        if form.is_valid() and formset.is_valid():
            prato = form.save(commit=False)
            prato.usuario = request.user
            prato.save()
            formset.instance = prato
            formset.save()
            messages.success(request, 'Prato cadastrado com sucesso.')
            return redirect('prato_list')
    else:
        form = PratoForm()
        formset = ItemFichaTecnicaFormSet(form_kwargs={'usuario': request.user})
    return render(request, 'prato_form.html', {'form': form, 'formset': formset})


@login_required
def prato_update(request, pk):
    prato = get_object_or_404(Prato, pk=pk, usuario=request.user)
    if request.method == 'POST':
        form = PratoForm(request.POST, instance=prato)
        formset = ItemFichaTecnicaFormSet(request.POST, instance=prato, form_kwargs={'usuario': request.user})
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, 'Prato atualizado com sucesso.')
            return redirect('prato_list')
    else:
        form = PratoForm(instance=prato)
        formset = ItemFichaTecnicaFormSet(instance=prato, form_kwargs={'usuario': request.user})
    return render(request, 'prato_form.html', {'form': form, 'formset': formset})


@login_required
def prato_delete(request, pk):
    prato = get_object_or_404(Prato, pk=pk, usuario=request.user)
    if request.method == 'POST':
        prato.delete()
        messages.success(request, 'Prato excluído com sucesso.')
        return redirect('prato_list')
    return render(request, 'prato_confirm_delete.html', {'prato': prato})


@login_required
def prato_vender(request, pk):
    """Registra a venda de balcão de 1 unidade do prato: baixa o estoque de cada
    insumo da ficha técnica (uma MovimentacaoEstoque de saída por insumo) e cria
    um pedido já entregue, para a receita entrar no Financeiro. Tudo numa única
    transação, com os produtos travados: ou baixa tudo, ou não baixa nada."""
    prato = get_object_or_404(Prato, pk=pk, usuario=request.user)

    if request.method != 'POST':
        return redirect('prato_list')

    if not prato.itens_ficha_tecnica.exists():
        messages.error(request, f'"{prato.nome}" não tem ficha técnica cadastrada.')
        return redirect('prato_list')

    try:
        servicos.vender_prato(request.user, prato)
    except servicos.EstoqueInsuficiente as e:
        messages.error(request, f'Estoque insuficiente para vender "{prato.nome}": faltando {e}.')
    else:
        messages.success(request, f'Venda de "{prato.nome}" registrada. Estoque atualizado.')
    return redirect('prato_list')


# ---------- PAINEL DE USUÁRIOS (acesso restrito por usuário e senha) ----------
# Área separada do login normal do sistema (não usa contas de restaurante).
# Serve para você (dona do TCC) ver todas as contas já cadastradas e excluir
# alguma se precisar. Login próprio guardado na sessão do navegador.

PAINEL_USUARIOS_SESSION_KEY = 'painel_usuarios_liberado'
PAINEL_MAX_TENTATIVAS = 5
PAINEL_JANELA_SEGUNDOS = 15 * 60


def _credenciais_painel_usuarios():
    """Lê usuário/senha do painel das variáveis de ambiente ADMIN_USERNAME e
    ADMIN_PASSWORD. Sem elas o painel fica DESATIVADO (não existe senha padrão)."""
    return os.environ.get('ADMIN_USERNAME', ''), os.environ.get('ADMIN_PASSWORD', '')


def _iguais(a, b):
    return secrets.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def painel_usuarios_login(request):
    """Tela de login (usuário + senha) do painel. Se as credenciais digitadas
    baterem, libera a sessão do navegador para acessar a lista de usuários."""
    if request.session.get(PAINEL_USUARIOS_SESSION_KEY):
        return redirect('painel_usuarios_list')

    erro = None
    if request.method == 'POST':
        chave = f'painel:{ip_do_cliente(request)}'
        usuario_correto, senha_correta = _credenciais_painel_usuarios()

        if excedeu_limite(chave, PAINEL_MAX_TENTATIVAS, PAINEL_JANELA_SEGUNDOS):
            erro = 'Muitas tentativas. Aguarde 15 minutos para tentar de novo.'
        elif not usuario_correto or not senha_correta:
            erro = 'Painel desativado: defina ADMIN_USERNAME e ADMIN_PASSWORD no ambiente.'
        else:
            usuario_digitado = request.POST.get('usuario', '')
            senha_digitada = request.POST.get('senha', '')
            ok_usuario = _iguais(usuario_digitado, usuario_correto)
            ok_senha = _iguais(senha_digitada, senha_correta)  # sempre compara os dois
            if ok_usuario and ok_senha:
                limpar_limite(chave)
                request.session.cycle_key()
                request.session[PAINEL_USUARIOS_SESSION_KEY] = True
                return redirect('painel_usuarios_list')
            erro = 'Usuário ou senha incorretos.'

    return render(request, 'login_adm.html', {'erro': erro})


@require_POST
def painel_usuarios_logout(request):
    request.session.pop(PAINEL_USUARIOS_SESSION_KEY, None)
    return redirect('login_adm')


def painel_usuarios_list(request):
    """Lista todos os usuários (restaurantes) cadastrados no sistema,
    com data de cadastro, último login e quantos produtos cada um tem."""
    if not request.session.get(PAINEL_USUARIOS_SESSION_KEY):
        return redirect('login_adm')

    usuarios = (
        User.objects.all()
        .annotate(total_produtos=Count('produtos', distinct=True))
        .order_by('-date_joined')
    )
    return render(request, 'painel_usuarios_list.html', {'usuarios': usuarios})


def painel_usuarios_delete(request, pk):
    """Exclui um usuário. Como todos os modelos do sistema apontam pro
    usuário com on_delete=CASCADE, excluir aqui apaga também TODOS os
    produtos, reservas, fornecedores, despesas e pratos daquela conta.
    Contas de administrador (superusuário/staff) não podem ser excluídas por aqui."""
    if not request.session.get(PAINEL_USUARIOS_SESSION_KEY):
        return redirect('login_adm')

    usuario = get_object_or_404(User, pk=pk)

    if usuario.is_superuser or usuario.is_staff:
        messages.error(request, 'Contas de administrador não podem ser excluídas por este painel.')
        return redirect('painel_usuarios_list')

    if request.method == 'POST':
        nome = usuario.username
        usuario.delete()
        messages.success(request, f'Usuário "{nome}" e todos os seus dados foram excluídos.')
        return redirect('painel_usuarios_list')

    return render(request, 'painel_usuarios_confirm_delete.html', {'usuario': usuario})
