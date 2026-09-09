"""
Testes automatizados do sistema de estoque/restaurantes.

Cobrem principalmente:
1. Isolamento multi-tenant (o requisito mais crítico do projeto: dados de um
   restaurante NUNCA podem aparecer ou ser alteráveis por outro).
2. Regras de negócio do estoque: cálculo de valor, estoque excedente,
   validação de movimentação, venda de prato via ficha técnica.

Para rodar: python manage.py test restaurante_app
"""
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client

from .models import Produto, Fornecedor, Reserva, Prato, ItemFichaTecnica, MovimentacaoEstoque
from .forms import MovimentacaoEstoqueForm, ReservaForm


def _validade_futura(dias=30):
    return date.today() + timedelta(days=dias)


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
            data=date.today(), horario='19:00:00', quantidade_pessoas=2,
        )
        Reserva.objects.create(
            usuario=self.restaurante_b, mesa=1, cliente_nome='Cliente B',
            data=date.today(), horario='19:00:00', quantidade_pessoas=4,
        )
        resp = self.client_a.get('/reservas/')
        self.assertContains(resp, 'Cliente A')
        self.assertNotContains(resp, 'Cliente B')

    def test_mesma_mesa_mesmo_horario_permitido_em_restaurantes_diferentes(self):
        form_a = ReservaForm(data={
            'mesa': 1, 'cliente_nome': 'Cliente A', 'cliente_telefone': '',
            'data': date.today(), 'horario': '19:00', 'quantidade_pessoas': 2,
            'tipo_pagamento': 'dinheiro',
        }, usuario=self.restaurante_a)
        self.assertTrue(form_a.is_valid(), form_a.errors)
        reserva_a = form_a.save(commit=False)
        reserva_a.usuario = self.restaurante_a
        reserva_a.save()

        form_b = ReservaForm(data={
            'mesa': 1, 'cliente_nome': 'Cliente B', 'cliente_telefone': '',
            'data': date.today(), 'horario': '19:00', 'quantidade_pessoas': 4,
            'tipo_pagamento': 'dinheiro',
        }, usuario=self.restaurante_b)
        self.assertTrue(form_b.is_valid(), form_b.errors)

    def test_conflito_de_mesa_bloqueado_dentro_do_mesmo_restaurante(self):
        Reserva.objects.create(
            usuario=self.restaurante_a, mesa=1, cliente_nome='Cliente A',
            data=date.today(), horario='19:00:00', quantidade_pessoas=2,
        )
        form = ReservaForm(data={
            'mesa': 1, 'cliente_nome': 'Outro Cliente', 'cliente_telefone': '',
            'data': date.today(), 'horario': '19:00', 'quantidade_pessoas': 3,
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
            nome_fornecedor='Atacadão', preco_unidade=5,
        )
        self.assertEqual(self.produto.valor_em_estoque, 250)

    def test_estoque_excedente_true_acima_do_maximo(self):
        self.produto.quantidade = 100
        self.produto.save()
        self.assertTrue(self.produto.estoque_excedente)

    def test_estoque_excedente_false_dentro_do_limite(self):
        self.assertFalse(self.produto.estoque_excedente)

    def test_compra_gera_movimentacao_de_entrada_automaticamente(self):
        Fornecedor.objects.create(
            usuario=self.user, produto=self.produto,
            nome_fornecedor='Atacadão', preco_unidade=5,
        )
        mov = MovimentacaoEstoque.objects.get(produto=self.produto)
        self.assertEqual(mov.tipo, 'entrada')

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
