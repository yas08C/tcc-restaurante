"""
Testes automatizados do sistema de estoque/restaurantes.

Cobrem principalmente:
1. Isolamento multi-tenant (o requisito mais crítico do projeto: dados de um
   restaurante NUNCA podem aparecer ou ser alteráveis por outro).
2. Regras de negócio do estoque: cálculo de valor, estoque excedente,
   validação de movimentação, venda de prato via ficha técnica.

Para rodar: python manage.py test restaurante_app
"""
import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from . import servicos
from .forms import CadastroForm, DespesaFixaForm, MovimentacaoEstoqueForm, ProdutoForm, ReservaForm
from .models import (
    DespesaFixa, Fornecedor, ItemFichaTecnica, MovimentacaoEstoque, Notificacao,
    Pedido, Prato, Produto, Reserva, SensorESP32, ZonaTemperatura, perfil_de,
)
from .seguranca import token_mesa
from .utils import verificar_alertas_estoque


def _hoje():
    return timezone.localdate()


def _validade_futura(dias=30):
    return _hoje() + timedelta(days=dias)


def _futuro(dias=2):
    """Data futura segura para testes de reserva."""
    return _hoje() + timedelta(days=dias)


class LimpaCacheMixin:
    def setUp(self):
        super().setUp()
        cache.clear()


class IsolamentoMultiTenantTests(TestCase):
    """O requisito nº 1 do projeto: dados de um restaurante não podem se
    misturar com os de outro."""

    def setUp(self):
        self.restaurante_a = User.objects.create_user('restaurante_a', password='senha123')
        self.restaurante_b = User.objects.create_user('restaurante_b', password='senha123')

        self.produto_a = Produto.objects.create(
            usuario=self.restaurante_a, nome='Salmão A', quantidade=10, validade=_validade_futura()
        )
        self.produto_b = Produto.objects.create(
            usuario=self.restaurante_b, nome='Salmão B', quantidade=10, validade=_validade_futura()
        )

        self.client_a = Client()
        self.client_a.login(username='restaurante_a', password='senha123')

    def test_lista_de_estoque_nao_mostra_produto_de_outro_restaurante(self):
        resp = self.client_a.get('/produtos/')
        self.assertContains(resp, 'Salmão A')
        self.assertNotContains(resp, 'Salmão B')

    def test_nao_consegue_editar_produto_de_outro_restaurante_via_url(self):
        resp = self.client_a.get(f'/produtos/{self.produto_b.pk}/editar/')
        self.assertEqual(resp.status_code, 404)

    def test_nao_consegue_excluir_produto_de_outro_restaurante_via_url(self):
        resp = self.client_a.post(f'/produtos/{self.produto_b.pk}/excluir/')
        self.assertEqual(resp.status_code, 404)
        self.produto_b.refresh_from_db()

    def test_reserva_isolada_por_restaurante(self):
        Reserva.objects.create(
            usuario=self.restaurante_a, mesa=1, cliente_nome='Cliente A',
            data=_futuro(), horario='19:00:00', quantidade_pessoas=2,
        )
        Reserva.objects.create(
            usuario=self.restaurante_b, mesa=1, cliente_nome='Cliente B',
            data=_futuro(), horario='19:00:00', quantidade_pessoas=4,
        )
        resp = self.client_a.get('/reservas/')
        self.assertContains(resp, 'Cliente A')
        self.assertNotContains(resp, 'Cliente B')

    def test_mesma_mesa_mesmo_horario_permitido_em_restaurantes_diferentes(self):
        form_a = ReservaForm(data={
            'mesa': 1, 'cliente_nome': 'Cliente A', 'cliente_telefone': '',
            'data': _futuro(), 'horario': '19:00', 'quantidade_pessoas': 2,
            'tipo_pagamento': 'dinheiro',
        }, usuario=self.restaurante_a)
        self.assertTrue(form_a.is_valid(), form_a.errors)
        reserva_a = form_a.save(commit=False)
        reserva_a.usuario = self.restaurante_a
        reserva_a.save()

        form_b = ReservaForm(data={
            'mesa': 1, 'cliente_nome': 'Cliente B', 'cliente_telefone': '',
            'data': _futuro(), 'horario': '19:00', 'quantidade_pessoas': 4,
            'tipo_pagamento': 'dinheiro',
        }, usuario=self.restaurante_b)
        self.assertTrue(form_b.is_valid(), form_b.errors)

    def test_conflito_de_mesa_bloqueado_dentro_do_mesmo_restaurante(self):
        Reserva.objects.create(
            usuario=self.restaurante_a, mesa=1, cliente_nome='Cliente A',
            data=_futuro(), horario='19:00:00', quantidade_pessoas=2,
        )
        form = ReservaForm(data={
            'mesa': 1, 'cliente_nome': 'Outro Cliente', 'cliente_telefone': '',
            'data': _futuro(), 'horario': '19:00', 'quantidade_pessoas': 3,
            'tipo_pagamento': 'pix',
        }, usuario=self.restaurante_a)
        self.assertFalse(form.is_valid())


class EstoqueTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('chef', password='senha123')
        self.produto = Produto.objects.create(
            usuario=self.user, nome='Arroz', quantidade=50,
            quantidade_minima=10, quantidade_maxima=90,
            unidade='kg', validade=_validade_futura(),
        )

    def test_valor_em_estoque_sem_compra_registrada_e_none(self):
        self.assertIsNone(self.produto.valor_em_estoque)

    def test_valor_em_estoque_calculado_pela_ultima_compra(self):
        Fornecedor.objects.create(
            usuario=self.user, produto=self.produto,
            nome_fornecedor='Atacadão', quantidade=50, preco_unidade=5,
        )
        self.assertEqual(self.produto.valor_em_estoque, 250)

    def test_estoque_excedente_true_acima_do_maximo(self):
        self.produto.quantidade = 100
        self.produto.save()
        self.assertTrue(self.produto.estoque_excedente)

    def test_estoque_excedente_false_dentro_do_limite(self):
        self.assertFalse(self.produto.estoque_excedente)

    def test_compra_gera_movimentacao_de_entrada_automaticamente(self):
        servicos.registrar_compra(Fornecedor(
            usuario=self.user, produto=self.produto,
            nome_fornecedor='Atacadão', quantidade=20, preco_unidade=5,
        ))
        mov = MovimentacaoEstoque.objects.get(produto=self.produto)
        self.assertEqual(mov.tipo, 'entrada')
        self.assertEqual(mov.quantidade, 20)
        self.assertEqual(mov.quantidade_resultante, 70)

    def test_movimentacao_manual_nao_permite_saida_maior_que_estoque(self):
        form = MovimentacaoEstoqueForm(data={
            'produto': self.produto.pk, 'tipo': 'saida',
            'quantidade': 9999, 'observacao': 'teste',
        }, usuario=self.user)
        self.assertFalse(form.is_valid())

    def test_movimentacao_manual_valida_dentro_do_limite(self):
        form = MovimentacaoEstoqueForm(data={
            'produto': self.produto.pk, 'tipo': 'saida',
            'quantidade': 10, 'observacao': 'uso na cozinha',
        }, usuario=self.user)
        self.assertTrue(form.is_valid(), form.errors)


class FichaTecnicaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('chef', password='senha123')
        self.client = Client()
        self.client.login(username='chef', password='senha123')

        self.salmao = Produto.objects.create(
            usuario=self.user, nome='Salmão', quantidade=10, unidade='kg', validade=_validade_futura()
        )
        self.arroz = Produto.objects.create(
            usuario=self.user, nome='Arroz', quantidade=10, unidade='kg', validade=_validade_futura()
        )
        self.prato = Prato.objects.create(usuario=self.user, nome='Combo Salmão', preco_venda=45)
        ItemFichaTecnica.objects.create(prato=self.prato, produto=self.salmao, quantidade_usada=1)
        ItemFichaTecnica.objects.create(prato=self.prato, produto=self.arroz, quantidade_usada=1)

    def test_pode_ser_vendido_com_estoque_suficiente(self):
        self.assertTrue(self.prato.pode_ser_vendido())

    def test_nao_pode_ser_vendido_sem_estoque(self):
        self.salmao.quantidade = 0
        self.salmao.save()
        self.assertFalse(self.prato.pode_ser_vendido())

    def test_venda_baixa_estoque_de_todos_os_insumos(self):
        self.client.post(f'/pratos/{self.prato.pk}/vender/')
        self.salmao.refresh_from_db()
        self.arroz.refresh_from_db()
        self.assertEqual(self.salmao.quantidade, 9)
        self.assertEqual(self.arroz.quantidade, 9)

    def test_venda_cria_movimentacoes_de_saida(self):
        self.client.post(f'/pratos/{self.prato.pk}/vender/')
        self.assertEqual(
            MovimentacaoEstoque.objects.filter(produto=self.salmao, tipo='saida').count(), 1
        )

    def test_venda_bloqueada_sem_estoque_nao_altera_nada(self):
        self.salmao.quantidade = 0
        self.salmao.save()
        self.client.post(f'/pratos/{self.prato.pk}/vender/')
        self.arroz.refresh_from_db()
        self.assertEqual(self.arroz.quantidade, 10)


# ======================================================================
# Testes das correções
# ======================================================================

class AutenticacaoTests(LimpaCacheMixin, TestCase):
    """Login e cadastro davam erro 500 (include de template com nome errado)."""

    def test_pagina_de_login_abre(self):
        resp = self.client.get('/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Entrar')

    def test_pagina_de_cadastro_abre(self):
        self.assertEqual(self.client.get('/cadastro/').status_code, 200)

    def test_login_com_senha_correta_redireciona_para_home(self):
        User.objects.create_user('chef', password='senha-forte-123')
        resp = self.client.post('/login/', {'username': 'chef', 'password': 'senha-forte-123'})
        self.assertRedirects(resp, '/')

    def test_login_com_senha_errada_mostra_erro(self):
        User.objects.create_user('chef', password='senha-forte-123')
        resp = self.client.post('/login/', {'username': 'chef', 'password': 'errada'})
        self.assertContains(resp, 'incorretos')

    def test_cadastro_cria_usuario_e_perfil_publico(self):
        resp = self.client.post('/cadastro/', {
            'username': 'novo', 'password1': 'senha-forte-123', 'password2': 'senha-forte-123',
        })
        self.assertRedirects(resp, '/')
        usuario = User.objects.get(username='novo')
        self.assertTrue(perfil_de(usuario).slug_publico)

    def test_cadastro_ignora_maiusculas_no_usuario(self):
        User.objects.create_user('Yas', password='senha-forte-123')
        form = CadastroForm(data={'username': 'yas', 'password1': 'senha-forte-123', 'password2': 'senha-forte-123'})
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_logout_exige_post(self):
        User.objects.create_user('chef', password='senha-forte-123')
        self.client.login(username='chef', password='senha-forte-123')
        self.client.get('/logout/')  # um link/imagem de terceiros não pode deslogar
        self.assertEqual(self.client.get('/').status_code, 200)
        self.client.post('/logout/')
        self.assertEqual(self.client.get('/').status_code, 302)


class PainelUsuariosTests(LimpaCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alvo = User.objects.create_user('restaurante', password='x-senha-123')
        self.admin = User.objects.create_superuser('dono', password='x-senha-123')
        patcher = mock.patch.dict(os.environ, {'ADMIN_USERNAME': 'painel', 'ADMIN_PASSWORD': 'segredo-longo'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entrar(self):
        self.client.post('/painel-usuarios/', {'usuario': 'painel', 'senha': 'segredo-longo'})

    def test_sem_variaveis_de_ambiente_o_painel_fica_desativado(self):
        with mock.patch.dict(os.environ, {'ADMIN_USERNAME': '', 'ADMIN_PASSWORD': ''}):
            resp = self.client.post('/painel-usuarios/', {'usuario': 'tcc', 'senha': 'yasmin123'})
        self.assertContains(resp, 'desativado')
        self.assertEqual(self.client.get('/painel-usuarios/lista/').status_code, 302)

    def test_senha_antiga_padrao_nao_funciona(self):
        resp = self.client.post('/painel-usuarios/', {'usuario': 'tcc', 'senha': 'yasmin123'})
        self.assertContains(resp, 'incorretos')

    def test_bloqueia_apos_muitas_tentativas(self):
        for _ in range(5):
            self.client.post('/painel-usuarios/', {'usuario': 'painel', 'senha': 'errada'})
        resp = self.client.post('/painel-usuarios/', {'usuario': 'painel', 'senha': 'segredo-longo'})
        self.assertContains(resp, 'Muitas tentativas')

    def test_get_na_exclusao_mostra_confirmacao_e_nao_da_500(self):
        self._entrar()
        resp = self.client.get(f'/painel-usuarios/{self.alvo.pk}/excluir/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.alvo.pk).exists())

    def test_post_exclui_usuario(self):
        self._entrar()
        self.client.post(f'/painel-usuarios/{self.alvo.pk}/excluir/')
        self.assertFalse(User.objects.filter(pk=self.alvo.pk).exists())

    def test_nao_exclui_superusuario(self):
        self._entrar()
        self.client.post(f'/painel-usuarios/{self.admin.pk}/excluir/')
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())


class ComprasEEstoqueTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('chef', password='senha123')
        self.client.login(username='chef', password='senha123')
        self.produto = Produto.objects.create(
            usuario=self.user, nome='Arroz', quantidade=10, unidade='kg', validade=_validade_futura())

    def test_compra_soma_ao_estoque_e_preco_total_usa_a_propria_quantidade(self):
        resp = self.client.post('/fornecedores/novo/', {
            'produto': self.produto.pk, 'nome_fornecedor': 'Atacadão',
            'quantidade': '5', 'preco_unidade': '4.00',
        })
        self.assertEqual(resp.status_code, 302)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 15)
        compra = Fornecedor.objects.get()
        self.assertEqual(compra.preco_total, Decimal('20.00'))

    def test_editar_compra_antiga_nao_muda_o_valor_pago(self):
        servicos.registrar_compra(Fornecedor(
            usuario=self.user, produto=self.produto, nome_fornecedor='A', quantidade=5, preco_unidade=4))
        compra = Fornecedor.objects.get()
        self.produto.refresh_from_db()
        self.produto.quantidade = 100  # estoque muda depois (vendas, compras...)
        self.produto.save()
        self.client.post(f'/fornecedores/{compra.pk}/editar/', {
            'nome_fornecedor': 'A (corrigido)', 'preco_unidade': '4.00',
            'produto': self.produto.pk, 'quantidade': '999',  # campos travados: ignorados
        })
        compra.refresh_from_db()
        self.assertEqual(compra.preco_total, Decimal('20.00'))
        self.assertEqual(compra.quantidade, 5)
        self.assertEqual(compra.nome_fornecedor, 'A (corrigido)')

    def test_excluir_produto_preserva_historico_financeiro(self):
        servicos.registrar_compra(Fornecedor(
            usuario=self.user, produto=self.produto, nome_fornecedor='A', quantidade=5, preco_unidade=4))
        self.client.post(f'/produtos/{self.produto.pk}/excluir/')
        compra = Fornecedor.objects.get()
        self.assertEqual(compra.preco_total, Decimal('20.00'))
        self.assertEqual(compra.nome_produto, 'Arroz')
        self.assertTrue(MovimentacaoEstoque.objects.filter(nome_produto='Arroz').exists())

    def test_quantidades_decimais(self):
        form = MovimentacaoEstoqueForm(data={
            'produto': self.produto.pk, 'tipo': 'saida', 'quantidade': '0.5', 'observacao': ''}, usuario=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        servicos.aplicar_movimentacao(self.user, self.produto.pk, 'saida', Decimal('0.5'))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal('9.5'))

    def test_movimentacao_nao_deixa_estoque_negativo(self):
        with self.assertRaises(servicos.EstoqueInsuficiente):
            servicos.aplicar_movimentacao(self.user, self.produto.pk, 'saida', Decimal('11'))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

    def test_movimentacao_via_tela_registra_saida(self):
        resp = self.client.post('/estoque/movimentar/', {
            'produto': self.produto.pk, 'tipo': 'saida', 'quantidade': '3', 'observacao': 'uso'})
        self.assertEqual(resp.status_code, 302)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 7)

    def test_valores_de_estoque_sem_query_por_produto(self):
        for i in range(5):
            p = Produto.objects.create(usuario=self.user, nome=f'P{i}', quantidade=1, validade=_validade_futura())
            Fornecedor.objects.create(usuario=self.user, produto=p, nome_fornecedor='F', quantidade=1, preco_unidade=2)
        with self.assertNumQueries(3):  # sessão, usuário e UMA query de produtos (com o último preço junto)
            self.client.get('/produtos/')

    def test_validacoes_de_produto(self):
        base = {'nome': 'Novo', 'categoria': 'outros', 'quantidade': '1', 'quantidade_minima': '5',
                'quantidade_maxima': '10', 'unidade': 'kg', 'validade': _validade_futura().isoformat()}
        self.assertTrue(ProdutoForm(data=base, usuario=self.user).is_valid())
        self.assertFalse(ProdutoForm(data={**base, 'quantidade_maxima': '2'}, usuario=self.user).is_valid())
        self.assertFalse(ProdutoForm(data={**base, 'validade': (_hoje() - timedelta(days=1)).isoformat()},
                                     usuario=self.user).is_valid())
        self.assertFalse(ProdutoForm(data={**base, 'nome': 'arroz'}, usuario=self.user).is_valid())  # repetido


class FichaTecnicaEVendaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('chef', password='senha123')
        self.client.login(username='chef', password='senha123')
        self.salmao = Produto.objects.create(usuario=self.user, nome='Salmão', quantidade=2, unidade='kg',
                                             validade=_validade_futura())
        self.prato = Prato.objects.create(usuario=self.user, nome='Sashimi', preco_venda=40)
        ItemFichaTecnica.objects.create(prato=self.prato, produto=self.salmao, quantidade_usada=Decimal('0.5'))

    def test_prato_sem_ficha_tecnica_nao_e_vendavel(self):
        vazio = Prato.objects.create(usuario=self.user, nome='Misterioso', preco_venda=10)
        self.assertFalse(vazio.pode_ser_vendido())

    def test_venda_decimal_baixa_estoque_e_gera_receita(self):
        self.client.post(f'/pratos/{self.prato.pk}/vender/')
        self.salmao.refresh_from_db()
        self.assertEqual(self.salmao.quantidade, Decimal('1.5'))
        pedido = Pedido.objects.get()
        self.assertEqual((pedido.status, pedido.mesa), ('entregue', None))
        resp = self.client.get('/financeiro/')
        self.assertContains(resp, 'Receita de pedidos entregues')
        self.assertEqual(resp.context['resumo_mensal'][0]['receita_pedidos'], Decimal('40'))

    def test_venda_sem_estoque_nao_cria_pedido_nem_movimentacao(self):
        self.salmao.quantidade = Decimal('0.2')
        self.salmao.save()
        self.client.post(f'/pratos/{self.prato.pk}/vender/')
        self.assertEqual(Pedido.objects.count(), 0)
        self.assertEqual(MovimentacaoEstoque.objects.count(), 0)

    def test_venda_conferida_dentro_da_transacao_nao_fica_negativa(self):
        servicos.vender_prato(self.user, self.prato)
        servicos.vender_prato(self.user, self.prato)
        servicos.vender_prato(self.user, self.prato)
        servicos.vender_prato(self.user, self.prato)  # 2 kg = 4 vendas de 0,5
        with self.assertRaises(servicos.EstoqueInsuficiente):
            servicos.vender_prato(self.user, self.prato)
        self.salmao.refresh_from_db()
        self.assertEqual(self.salmao.quantidade, 0)


class AlertasTests(LimpaCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('chef', password='senha123')

    def _produto(self, **kw):
        dados = dict(usuario=self.user, nome='Item', quantidade=10, quantidade_minima=2, validade=_validade_futura())
        dados.update(kw)
        return Produto.objects.create(**dados)

    def test_produto_vencido_gera_alerta_e_nao_e_resolvido(self):
        p = self._produto(validade=_hoje() - timedelta(days=2))
        verificar_alertas_estoque(self.user, forcar=True)
        n = Notificacao.objects.get(tipo='validade', produto=p)
        self.assertFalse(n.resolvida)
        self.assertIn('venceu', n.mensagem)
        verificar_alertas_estoque(self.user, forcar=True)
        n.refresh_from_db()
        self.assertFalse(n.resolvida)

    def test_alerta_resolve_quando_problema_passa(self):
        p = self._produto(quantidade=1)  # abaixo do mínimo
        verificar_alertas_estoque(self.user, forcar=True)
        self.assertTrue(Notificacao.objects.filter(tipo='estoque', resolvida=False).exists())
        p.quantidade = 50
        p.save()
        verificar_alertas_estoque(self.user, forcar=True)
        self.assertFalse(Notificacao.objects.filter(tipo='estoque', resolvida=False).exists())

    def test_numero_de_queries_nao_cresce_com_o_numero_de_produtos(self):
        for i in range(20):
            self._produto(nome=f'P{i}', quantidade=1)
        with self.assertNumQueries(3):  # notificações abertas, produtos e UM bulk_create
            verificar_alertas_estoque(self.user, forcar=True)

    def test_verificacao_nao_roda_a_cada_requisicao(self):
        self._produto(quantidade=1)
        verificar_alertas_estoque(self.user)
        with self.assertNumQueries(0):
            verificar_alertas_estoque(self.user)


class ReservaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('chef', password='senha123')

    def _form(self, **kw):
        dados = {'mesa': 1, 'cliente_nome': 'Ana', 'cliente_telefone': '', 'data': _futuro(),
                 'horario': '19:00', 'quantidade_pessoas': 2, 'tipo_pagamento': 'pix'}
        dados.update(kw)
        return ReservaForm(data=dados, usuario=self.user)

    def test_data_no_passado_recusada(self):
        self.assertFalse(self._form(data=_hoje() - timedelta(days=1)).is_valid())

    def test_zero_pessoas_recusado(self):
        self.assertFalse(self._form(quantidade_pessoas=0).is_valid())

    def test_horarios_sobrepostos_na_mesma_mesa_recusados(self):
        Reserva.objects.create(usuario=self.user, mesa=1, cliente_nome='X', data=_futuro(),
                               horario='19:00', quantidade_pessoas=2)
        self.assertFalse(self._form(horario='19:30').is_valid())
        self.assertTrue(self._form(horario='21:00').is_valid())
        self.assertTrue(self._form(horario='19:30', mesa=2).is_valid())

    def test_madrugada_pertence_ao_expediente_do_dia_anterior(self):
        Reserva.objects.create(usuario=self.user, mesa=1, cliente_nome='X', data=_futuro(),
                               horario='00:30', quantidade_pessoas=2)
        self.assertFalse(self._form(horario='00:00').is_valid())      # mesma madrugada
        self.assertTrue(self._form(horario='18:00').is_valid())       # início do expediente
        # 00:30 do expediente D acontece em D+1; não conflita com 19:00 de D+1
        self.assertTrue(self._form(data=_futuro(3), horario='19:00').is_valid())

    def test_despesa_duplicada_recusada_exceto_outros(self):
        DespesaFixa.objects.create(usuario=self.user, tipo='aluguel', valor=1000, mes=1, ano=2026)
        dup = DespesaFixaForm(data={'tipo': 'aluguel', 'valor': '900', 'mes': 1, 'ano': 2026}, usuario=self.user)
        self.assertFalse(dup.is_valid())
        DespesaFixa.objects.create(usuario=self.user, tipo='outros', valor=10, mes=1, ano=2026)
        outros = DespesaFixaForm(data={'tipo': 'outros', 'valor': '20', 'mes': 1, 'ano': 2026}, usuario=self.user)
        self.assertTrue(outros.is_valid(), outros.errors)


class ApiTemperaturaTests(LimpaCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('chef', password='senha123')
        self.sensor_a = SensorESP32.objects.create(usuario=self.user, nome='Freezer', tipo_sensor='DS18B20', zona='A')
        self.sensor_b = SensorESP32.objects.create(usuario=self.user, nome='Seco', tipo_sensor='DHT22', zona='B')

    def _enviar(self, sensor, zona, temperatura, **extra):
        corpo = {'token': sensor.token, 'zona': zona, 'temperatura': temperatura}
        corpo.update(extra)
        return self.client.post('/api/temperatura/', data=json.dumps(corpo), content_type='application/json')

    def test_leitura_valida(self):
        resp = self._enviar(self.sensor_a, 'A', -18.5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ZonaTemperatura.objects.get(zona='A').temp_atual, Decimal('-18.5'))

    def test_valores_invalidos_dao_400_e_nao_500(self):
        for ruim in ['NaN', 'Infinity', float('inf'), 99999, -1000, 'abc', None, True, [1], {'a': 1}]:
            corpo = '{"token": "%s", "zona": "A", "temperatura": %s}' % (self.sensor_a.token, json.dumps(ruim))
            resp = self.client.post('/api/temperatura/', data=corpo, content_type='application/json')
            self.assertEqual(resp.status_code, 400, ruim)

    def test_nan_literal_do_json_python_tambem_e_recusado(self):
        corpo = '{"token": "%s", "zona": "A", "temperatura": NaN}' % self.sensor_a.token
        resp = self.client.post('/api/temperatura/', data=corpo, content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_zona_ou_token_com_tipo_errado_nao_da_500(self):
        resp = self.client.post('/api/temperatura/', data=json.dumps(
            {'token': self.sensor_a.token, 'zona': ['A'], 'temperatura': 1}), content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post('/api/temperatura/', data='[1,2]', content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_token_invalido_e_zona_travada(self):
        resp = self.client.post('/api/temperatura/', data=json.dumps(
            {'token': 'x', 'zona': 'A', 'temperatura': 1}), content_type='application/json')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self._enviar(self.sensor_a, 'B', 5).status_code, 403)

    def test_faixa_inicial_depende_do_tipo_do_sensor(self):
        self._enviar(self.sensor_a, 'A', -18)
        self._enviar(self.sensor_b, 'B', 5)
        freezer = ZonaTemperatura.objects.get(zona='A')
        seca = ZonaTemperatura.objects.get(zona='B')
        self.assertEqual((freezer.temp_minima, freezer.temp_maxima), (Decimal('-25'), Decimal('-15')))
        self.assertEqual((seca.temp_minima, seca.temp_maxima), (Decimal('0'), Decimal('8')))
        self.assertFalse(freezer.em_alerta)  # -18 °C num freezer é normal (antes gerava alerta falso)

    def test_limite_de_requisicoes_por_token(self):
        codigos = [self._enviar(self.sensor_a, 'A', -18).status_code for _ in range(22)]
        self.assertIn(429, codigos)

    def test_tela_atual_em_json(self):
        self._enviar(self.sensor_a, 'A', -18)
        self.client.login(username='chef', password='senha123')
        dados = self.client.get('/api/temperatura/atual/').json()
        self.assertEqual(dados['zonas'][0]['zona'], 'A')


class InterfaceClienteTests(LimpaCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('restaurante', password='senha123')
        self.slug = perfil_de(self.user).slug_publico
        self.produto = Produto.objects.create(usuario=self.user, nome='Arroz', quantidade=10, unidade='kg',
                                              validade=_validade_futura())
        self.prato = Prato.objects.create(usuario=self.user, nome='Temaki', preco_venda=30)
        ItemFichaTecnica.objects.create(prato=self.prato, produto=self.produto, quantidade_usada=1)
        self.url = f'/cliente/{self.slug}/'

    def _escanear_qr(self, mesa=5):
        return self.client.get(f'{self.url}?mesa={mesa}&t={token_mesa(self.user.pk, mesa)}')

    def _pedir(self, vezes=1):
        for _ in range(vezes):
            self.client.post(f'{self.url}adicionar/{self.prato.pk}/')
        return self.client.post(f'{self.url}confirmar/')

    def test_url_nao_expoe_o_login_do_restaurante(self):
        self.assertEqual(self.client.get('/cliente/restaurante/').status_code, 404)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'restaurante')

    def test_sem_qr_nao_da_para_pedir(self):
        self.client.post(f'{self.url}adicionar/{self.prato.pk}/')
        resp = self.client.post(f'{self.url}confirmar/')
        self.assertEqual(Pedido.objects.count(), 0)
        self.assertEqual(resp.status_code, 302)

    def test_qr_com_assinatura_falsa_e_recusado(self):
        self.client.get(f'{self.url}?mesa=5&t=falso')
        self.client.post(f'{self.url}adicionar/{self.prato.pk}/')
        self._pedir()
        self.assertEqual(Pedido.objects.count(), 0)

    def test_assinatura_de_uma_mesa_nao_vale_para_outra(self):
        self.client.get(f'{self.url}?mesa=6&t={token_mesa(self.user.pk, 5)}')
        self._pedir()
        self.assertEqual(Pedido.objects.count(), 0)

    def test_pedido_nao_baixa_estoque_ate_o_restaurante_confirmar(self):
        self._escanear_qr()
        self._pedir(2)
        pedido = Pedido.objects.get()
        self.assertEqual((pedido.status, pedido.mesa), ('recebido', 5))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

        self.client.login(username='restaurante', password='senha123')
        self.client.post(f'/pedidos/{pedido.pk}/confirmar/')
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 8)
        self.assertEqual(Pedido.objects.get().status, 'confirmado')

    def test_cancelar_pedido_confirmado_devolve_estoque(self):
        self._escanear_qr()
        self._pedir(3)
        pedido = Pedido.objects.get()
        servicos.confirmar_pedido(self.user, pedido.pk)
        servicos.cancelar_pedido(self.user, pedido.pk)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertEqual(Pedido.objects.get().status, 'cancelado')
        self.assertTrue(MovimentacaoEstoque.objects.filter(tipo='estorno').exists())
        with self.assertRaises(servicos.PedidoInvalido):
            servicos.cancelar_pedido(self.user, pedido.pk)  # não devolve duas vezes

    def test_confirmar_sem_estoque_nao_altera_nada(self):
        self._escanear_qr()
        self._pedir(3)
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=1)
        with self.assertRaises(servicos.EstoqueInsuficiente):
            servicos.confirmar_pedido(self.user, Pedido.objects.get().pk)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 1)
        self.assertEqual(Pedido.objects.get().status, 'recebido')

    def test_limite_de_pedidos_pendentes_por_mesa(self):
        self._escanear_qr()
        for _ in range(5):
            self._pedir()
        self.assertEqual(Pedido.objects.count(), 3)

    def test_entregue_entra_na_receita_apenas_depois_de_confirmar(self):
        self._escanear_qr()
        self._pedir()
        pedido = Pedido.objects.get()
        self.client.login(username='restaurante', password='senha123')
        self.client.post(f'/pedidos/{pedido.pk}/entregar/')  # ainda não confirmado: recusado
        self.assertEqual(Pedido.objects.get().status, 'recebido')
        servicos.confirmar_pedido(self.user, pedido.pk)
        self.client.post(f'/pedidos/{pedido.pk}/entregar/')
        self.assertEqual(Pedido.objects.get().status, 'entregue')
        resp = self.client.get('/financeiro/')
        self.assertEqual(resp.context['resumo_mensal'][0]['receita_pedidos'], Decimal('30'))

    def test_lista_de_pedidos_mostra_links_assinados_das_mesas(self):
        self.client.login(username='restaurante', password='senha123')
        resp = self.client.get('/pedidos/')
        self.assertContains(resp, token_mesa(self.user.pk, 7))


class TelasEAdminTests(TestCase):
    """Fumaça: nenhuma tela principal pode dar 500."""

    def setUp(self):
        self.user = User.objects.create_superuser('dono', password='senha123')
        self.client.login(username='dono', password='senha123')

    def test_telas_principais(self):
        for nome in ['home', 'produto_list', 'produto_create', 'estoque_historico', 'movimentacao_create',
                     'prato_list', 'prato_create', 'fornecedor_list', 'fornecedor_create', 'reserva_list',
                     'reserva_create', 'financeiro', 'despesa_list', 'despesa_create', 'temperatura',
                     'pedido_list', 'notificacoes_ativas']:
            self.assertEqual(self.client.get(reverse(nome)).status_code, 200, nome)

    def test_admin_lista_e_adiciona_todos_os_modelos(self):
        from django.contrib import admin
        for modelo in admin.site._registry:
            rotulo = f'admin:{modelo._meta.app_label}_{modelo._meta.model_name}'
            self.assertEqual(self.client.get(reverse(f'{rotulo}_changelist')).status_code, 200, rotulo)
            self.assertEqual(self.client.get(reverse(f'{rotulo}_add')).status_code, 200, rotulo)
