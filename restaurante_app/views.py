import json
import os
from collections import defaultdict
from django.db.models import Sum, Count

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Sum
from django.db.models.functions import ExtractYear, ExtractMonth
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    Produto, Reserva, Fornecedor, ZonaTemperatura, SensorESP32, DespesaFixa,
    Notificacao, MovimentacaoEstoque, Prato, ItemFichaTecnica,
)
from .forms import (
    ProdutoForm, ReservaForm, FornecedorForm, DespesaFixaForm,
    MovimentacaoEstoqueForm, PratoForm, ItemFichaTecnicaFormSet,
)
from .utils import verificar_alertas_estoque
from django.db import transaction


# ---------- AUTENTICAÇÃO ----------

def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('home')
    else:
        form = AuthenticationForm()
    return render(request, 'login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


def cadastro_view(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('home')
    else:
        form = UserCreationForm()
    return render(request, 'cadastro.html', {'form': form})


# ---------- HOME (interface inicial) ----------

@login_required
def home(request):
    return render(request, 'home.html')


# ---------- ESTOQUE / PRODUTOS (por usuário) ----------

@login_required
def produto_list(request):
    produtos = Produto.objects.filter(usuario=request.user)

    categoria_selecionada = request.GET.get('categoria', '')
    if categoria_selecionada:
        produtos = produtos.filter(categoria=categoria_selecionada)

    # Valor total do estoque parado, estimado pelo preço da última compra de
    # cada produto (produtos sem nenhuma compra registrada não entram na soma).
    valor_total_estoque = sum(
        (p.valor_em_estoque for p in produtos if p.valor_em_estoque is not None),
        start=0,
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
    movimentacoes = MovimentacaoEstoque.objects.filter(usuario=request.user)
    return render(request, 'estoque_historico.html', {'movimentacoes': movimentacoes})


@login_required
def movimentacao_create(request):
    if request.method == 'POST':
        form = MovimentacaoEstoqueForm(request.POST, usuario=request.user)
        if form.is_valid():
            movimentacao = form.save(commit=False)
            movimentacao.usuario = request.user
            produto = movimentacao.produto

            if movimentacao.tipo == 'ajuste':
                # Ajuste de contagem: a quantidade informada é o novo total.
                produto.quantidade = movimentacao.quantidade
            else:
                # Saída ou perda: reduz a quantidade atual.
                produto.quantidade -= movimentacao.quantidade

            produto.save(update_fields=['quantidade'])
            movimentacao.quantidade_resultante = produto.quantidade
            movimentacao.save()

            messages.success(request, 'Movimentação de estoque registrada com sucesso.')
            return redirect('estoque_historico')
    else:
        form = MovimentacaoEstoqueForm(usuario=request.user)
    return render(request, 'movimentacao_form.html', {'form': form})


@login_required
def produto_create(request):
    if request.method == 'POST':
        form = ProdutoForm(request.POST)
        if form.is_valid():
            produto = form.save(commit=False)
            produto.usuario = request.user
            produto.save()
            messages.success(request, 'Produto cadastrado com sucesso.')
            return redirect('produto_list')
    else:
        form = ProdutoForm()
    return render(request, 'produto_form.html', {'form': form})


@login_required
def produto_update(request, pk):
    produto = get_object_or_404(Produto, pk=pk, usuario=request.user)
    if request.method == 'POST':
        form = ProdutoForm(request.POST, instance=produto)
        if form.is_valid():
            form.save()
            messages.success(request, 'Produto atualizado com sucesso.')
            return redirect('produto_list')
    else:
        form = ProdutoForm(instance=produto)
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
    fornecedores = Fornecedor.objects.filter(usuario=request.user)
    return render(request, 'fornecedor_list.html', {'fornecedores': fornecedores})


@login_required
def fornecedor_create(request):
    if request.method == 'POST':
        form = FornecedorForm(request.POST, usuario=request.user)
        if form.is_valid():
            fornecedor = form.save(commit=False)
            fornecedor.usuario = request.user
            fornecedor.save()
            messages.success(request, 'Compra registrada com sucesso.')
            return redirect('fornecedor_list')
    else:
        form = FornecedorForm(usuario=request.user)
    produtos_qtd = {p.id: float(p.quantidade) for p in Produto.objects.filter(usuario=request.user)}
    return render(request, 'fornecedor_form.html', {'form': form, 'produtos_qtd': produtos_qtd})


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
    produtos_qtd = {p.id: float(p.quantidade) for p in Produto.objects.filter(usuario=request.user)}
    return render(request, 'fornecedor_form.html', {'form': form, 'produtos_qtd': produtos_qtd})


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
        'total_gasto': 0, 'aluguel': 0, 'total_contas': 0,
        'total_pessoas': 0, 'qtd_reservas_pagas': 0,
    })

    # Compras de produtos (Fornecedor), agrupadas por mês/ano da compra
    compras_por_mes = (
        Fornecedor.objects.filter(usuario=request.user)
        .annotate(ano=ExtractYear('criado_em'), mes=ExtractMonth('criado_em'))
        .values('ano', 'mes')
        .annotate(total=Sum('preco_total'))
    )
    for c in compras_por_mes:
        resumo[(c['ano'], c['mes'])]['total_gasto'] += c['total']

    # Despesas fixas (aluguel/água/energia/outros) já guardam ano/mes próprios
    for d in DespesaFixa.objects.filter(usuario=request.user):
        if d.tipo == 'aluguel':
            resumo[(d.ano, d.mes)]['aluguel'] += d.valor
        else:
            resumo[(d.ano, d.mes)]['total_contas'] += d.valor

    # Reservas pagas, agrupadas pelo mês/ano da DATA da reserva
    reservas_por_mes = (
        Reserva.objects.filter(usuario=request.user, pago=True)
        .annotate(ano=ExtractYear('data'), mes=ExtractMonth('data'))
        .values('ano', 'mes')
        .annotate(total_pessoas=Sum('quantidade_pessoas'), qtd=Count('id'))
    )
    for r in reservas_por_mes:
        resumo[(r['ano'], r['mes'])]['total_pessoas'] += r['total_pessoas']
        resumo[(r['ano'], r['mes'])]['qtd_reservas_pagas'] += r['qtd']

    nomes_meses = dict(DespesaFixa.MESES_CHOICES)
    resumo_mensal = []
    for (ano, mes), v in sorted(resumo.items(), key=lambda item: item[0], reverse=True):
        total_despesas_fixas = v['aluguel'] + v['total_contas']
        total_geral_despesas = v['total_gasto'] + total_despesas_fixas
        total_reservas = v['total_pessoas'] * Reserva.VALOR_POR_PESSOA
        saldo = total_reservas - total_geral_despesas
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
            'valor_por_pessoa': Reserva.VALOR_POR_PESSOA,
            'saldo': saldo,
            'is_mes_atual': (ano == hoje.year and mes == hoje.month),
        })

    context = {
        'mes_referencia': hoje,
        'resumo_mensal': resumo_mensal,
    }
    return render(request, 'financeiro.html', context)


# ---------- DESPESAS FIXAS (aluguel, água, energia, outros) ----------

@login_required
def despesa_list(request):
    despesas = DespesaFixa.objects.filter(usuario=request.user)

    # Resumo mensal com TODAS as despesas do restaurante: aluguel, água,
    # energia, outros (DespesaFixa) + compra de produtos (Fornecedor),
    # já somados e agrupados por mês/ano.
    resumo = defaultdict(lambda: {'aluguel': 0, 'agua': 0, 'energia': 0, 'outros': 0, 'produtos': 0})

    for d in despesas:
        resumo[(d.ano, d.mes)][d.tipo] += d.valor

    compras_por_mes = (
        Fornecedor.objects.filter(usuario=request.user)
        .annotate(ano=ExtractYear('criado_em'), mes=ExtractMonth('criado_em'))
        .values('ano', 'mes')
        .annotate(total=Sum('preco_total'))
    )
    for c in compras_por_mes:
        resumo[(c['ano'], c['mes'])]['produtos'] += c['total']

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
        form = DespesaFixaForm(request.POST)
        if form.is_valid():
            despesa = form.save(commit=False)
            despesa.usuario = request.user
            despesa.save()
            messages.success(request, 'Despesa cadastrada com sucesso.')
            return redirect('despesa_list')
    else:
        form = DespesaFixaForm(initial={'mes': timezone.localdate().month, 'ano': timezone.localdate().year})
    return render(request, 'despesa_form.html', {'form': form})


@login_required
def despesa_update(request, pk):
    despesa = get_object_or_404(DespesaFixa, pk=pk, usuario=request.user)
    if request.method == 'POST':
        form = DespesaFixaForm(request.POST, instance=despesa)
        if form.is_valid():
            form.save()
            messages.success(request, 'Despesa atualizada com sucesso.')
            return redirect('despesa_list')
    else:
        form = DespesaFixaForm(instance=despesa)
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

@login_required
def temperatura_view(request):
    zonas = ZonaTemperatura.objects.filter(usuario=request.user).order_by('zona')
    return render(request, 'restaurante/temperatura.html', {'zonas': zonas})


@csrf_exempt
@require_POST
def receber_temperatura(request):
    try:
        data = json.loads(request.body)
        token = data['token']
        zona = data['zona']
        temperatura = data['temperatura']
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'erro': 'payload inválido'}, status=400)

    if zona not in dict(ZonaTemperatura.ZONA_CHOICES):
        return JsonResponse({'erro': 'zona inválida'}, status=400)

    sensor = get_object_or_404(SensorESP32, token=token, ativo=True)

    zona_obj, _ = ZonaTemperatura.objects.get_or_create(
        usuario=sensor.usuario,
        zona=zona,
        defaults={'temp_minima': 0, 'temp_maxima': 8}
    )
    zona_obj.temp_atual = temperatura
    zona_obj.save(update_fields=['temp_atual', 'atualizado_em'])
    # ↑ esse .save() dispara o signal verificar_alerta_temperatura automaticamente

    return JsonResponse({'status': 'ok', 'alerta': zona_obj.em_alerta})


# ---------- NOTIFICAÇÕES (banner de alertas) ----------

@login_required
def notificacoes_ativas(request):
    """Endpoint consultado via JavaScript (a cada poucos segundos) para
    alimentar o banner de alertas. Também reaproveita a chamada para
    checar validade/estoque baixo, sem precisar de tarefa agendada."""
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
    """Registra a venda de 1 unidade do prato: baixa automaticamente o
    estoque de cada insumo da ficha técnica e cria uma MovimentacaoEstoque
    de saída para cada um. Tudo dentro de uma transação — ou baixa tudo
    corretamente, ou não baixa nada (evita estoque inconsistente)."""
    prato = get_object_or_404(Prato, pk=pk, usuario=request.user)

    if request.method != 'POST':
        return redirect('prato_list')

    itens = list(prato.itens_ficha_tecnica.select_related('produto'))
    if not itens:
        messages.error(request, f'"{prato.nome}" não tem ficha técnica cadastrada.')
        return redirect('prato_list')

    faltando = [
        item for item in itens if item.produto.quantidade < item.quantidade_usada
    ]
    if faltando:
        nomes = ', '.join(item.produto.nome for item in faltando)
        messages.error(request, f'Estoque insuficiente para vender "{prato.nome}": faltando {nomes}.')
        return redirect('prato_list')

    with transaction.atomic():
        for item in itens:
            produto = item.produto
            produto.quantidade -= item.quantidade_usada
            produto.save(update_fields=['quantidade'])
            MovimentacaoEstoque.objects.create(
                usuario=request.user,
                produto=produto,
                tipo='saida',
                quantidade=item.quantidade_usada,
                quantidade_resultante=produto.quantidade,
                observacao=f'Venda de 1x {prato.nome}',
            )

    messages.success(request, f'Venda de "{prato.nome}" registrada. Estoque atualizado.')
    return redirect('prato_list')


# ---------- PAINEL DE USUÁRIOS (acesso restrito por usuário e senha) ----------
# Área separada do login normal do sistema (não usa contas de restaurante).
# Serve para você (dona do TCC) ver todas as contas já cadastradas e excluir
# alguma se precisar. Login próprio guardado na sessão do navegador.

PAINEL_USUARIOS_SESSION_KEY = 'painel_usuarios_liberado'


def _credenciais_painel_usuarios():
    """Lê usuário/senha do painel de variáveis de ambiente (ADMIN_USERNAME e
    ADMIN_PASSWORD). Se não existirem (ex: rodando local sem configurar),
    usa 'TCC' / 'yasmin123' como padrão."""
    usuario = os.environ.get('ADMIN_USERNAME', 'TCC')
    senha = os.environ.get('ADMIN_PASSWORD', 'yasmin123')
    return usuario, senha


def painel_usuarios_login(request):
    """Tela de login (usuário + senha) do painel. Se as credenciais digitadas
    baterem, libera a sessão do navegador para acessar a lista de usuários."""
    if request.session.get(PAINEL_USUARIOS_SESSION_KEY):
        return redirect('painel_usuarios_list')

    erro = None
    if request.method == 'POST':
        usuario_digitado = request.POST.get('usuario', '')
        senha_digitada = request.POST.get('senha', '')
        usuario_correto, senha_correta = _credenciais_painel_usuarios()

        if usuario_digitado == usuario_correto and senha_digitada == senha_correta:
            request.session[PAINEL_USUARIOS_SESSION_KEY] = True
            return redirect('painel_usuarios_list')
        erro = 'Usuário ou senha incorretos.'

    return render(request, 'painel_usuarios_login.html', {'erro': erro})


def painel_usuarios_logout(request):
    request.session.pop(PAINEL_USUARIOS_SESSION_KEY, None)
    return redirect('painel_usuarios_login')


def painel_usuarios_list(request):
    """Lista todos os usuários (restaurantes) cadastrados no sistema,
    com data de cadastro, último login e quantos produtos cada um tem."""
    if not request.session.get(PAINEL_USUARIOS_SESSION_KEY):
        return redirect('painel_usuarios_login')

    usuarios = (
        User.objects.all()
        .annotate(total_produtos=Count('produtos', distinct=True))
        .order_by('-date_joined')
    )
    return render(request, 'painel_usuarios_list.html', {'usuarios': usuarios})


def painel_usuarios_delete(request, pk):
    """Exclui um usuário. Como todos os modelos do sistema apontam pro
    usuário com on_delete=CASCADE, excluir aqui apaga também TODOS os
    produtos, reservas, fornecedores, despesas e pratos daquela conta."""
    if not request.session.get(PAINEL_USUARIOS_SESSION_KEY):
        return redirect('painel_usuarios_login')

    usuario = get_object_or_404(User, pk=pk)

    if request.method == 'POST':
        nome = usuario.username
        usuario.delete()
        messages.success(request, f'Usuário "{nome}" e todos os seus dados foram excluídos.')
        return redirect('painel_usuarios_list')

    return render(request, 'painel_usuarios_confirm_delete.html', {'usuario': usuario})
