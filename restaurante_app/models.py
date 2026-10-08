import secrets
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

MESA_CHOICES = [(i, f'Mesa {i}') for i in range(1, 16)]

# Quantidades aceitam casas decimais (0,5 kg, 1,25 L...).
QTD = {'max_digits': 10, 'decimal_places': 2}


def gerar_slug_publico():
    return secrets.token_hex(5)


class PerfilRestaurante(models.Model):
    """Dados públicos do restaurante. O slug aleatório é usado nos links do
    cliente, para não expor o nome de usuário (login) na URL."""

    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='perfil')
    slug_publico = models.CharField(max_length=20, unique=True, default=gerar_slug_publico, editable=False)
    nome_exibicao = models.CharField(
        'Nome exibido ao cliente', max_length=80, blank=True,
        help_text='Aparece no topo do cardápio do cliente. Deixe em branco para não mostrar nome.',
    )

    class Meta:
        verbose_name = 'Perfil do Restaurante'
        verbose_name_plural = 'Perfis dos Restaurantes'

    def __str__(self):
        return f'Perfil de {self.usuario}'


def perfil_de(usuario):
    perfil, _ = PerfilRestaurante.objects.get_or_create(usuario=usuario)
    return perfil


class ProdutoQuerySet(models.QuerySet):
    def com_ultimo_preco(self):
        """Anota o preço da compra mais recente, evitando uma query por produto."""
        ultima = Fornecedor.objects.filter(produto=OuterRef('pk')).order_by('-criado_em', '-pk')
        return self.annotate(ultimo_preco=Subquery(ultima.values('preco_unidade')[:1]))


class Produto(models.Model):
    objects = ProdutoQuerySet.as_manager()

    UNIDADE_CHOICES = [
        ('kg', 'Quilograma (kg)'),
        ('l', 'Litro (L)'),
        ('un', 'Unidade'),
    ]

    CATEGORIA_CHOICES = [
        ('bebidas', 'Bebidas'),
        ('carnes', 'Carnes e Peixes'),
        ('laticinios', 'Laticínios'),
        ('hortifruti', 'Hortifruti'),
        ('graos_massas', 'Grãos e Massas'),
        ('limpeza', 'Limpeza'),
        ('descartaveis', 'Descartáveis'),
        ('outros', 'Outros'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='produtos')
    nome = models.CharField(max_length=120)
    categoria = models.CharField(
        'Categoria', max_length=20, choices=CATEGORIA_CHOICES, default='outros'
    )
    quantidade = models.DecimalField(default=0, validators=[MinValueValidator(0)], **QTD)
    quantidade_minima = models.DecimalField(
        'Quantidade mínima', default=5, validators=[MinValueValidator(0)],
        help_text='Quando o estoque chegar a esse valor (ou ficar abaixo dele), um alerta de estoque baixo é disparado.',
        **QTD
    )
    quantidade_maxima = models.DecimalField(
        'Quantidade máxima', null=True, blank=True, validators=[MinValueValidator(0)], **QTD,
        help_text='Acima desse valor, indica estoque excessivo (compra além do necessário). Opcional.'
    )
    unidade = models.CharField('Unidade', max_length=2, choices=UNIDADE_CHOICES, default='un')
    validade = models.DateField()
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['validade']
        constraints = [
            models.CheckConstraint(condition=Q(quantidade__gte=0), name='produto_quantidade_nao_negativa'),
        ]

    def __str__(self):
        return self.nome

    def clean(self):
        super().clean()
        if self.quantidade_maxima is not None and self.quantidade_minima is not None \
                and self.quantidade_maxima < self.quantidade_minima:
            raise ValidationError({
                'quantidade_maxima': 'A quantidade máxima não pode ser menor que a mínima.'
            })

    @property
    def vencido(self):
        return self.validade < timezone.localdate()

    @property
    def estoque_excedente(self):
        """True se a quantidade atual estiver acima do máximo definido."""
        if self.quantidade_maxima is None:
            return False
        return self.quantidade > self.quantidade_maxima

    @property
    def custo_unitario_atual(self):
        """Preço da unidade da compra mais recente registrada para este produto
        (Fornecedor), usado para estimar o valor do estoque parado."""
        if hasattr(self, 'ultimo_preco'):  # vem anotado por com_ultimo_preco()
            return self.ultimo_preco
        ultima_compra = self.compras.order_by('-criado_em', '-pk').first()
        return ultima_compra.preco_unidade if ultima_compra else None

    @property
    def valor_em_estoque(self):
        """Quantidade atual × custo unitário da última compra. None se nunca
        houve compra registrada (não dá pra estimar valor)."""
        custo = self.custo_unitario_atual
        if custo is None:
            return None
        return self.quantidade * custo


class Fornecedor(models.Model):
    """Registro de compra feita a um fornecedor. Cada registro pertence a um usuário.

    A compra guarda a PRÓPRIA quantidade comprada: o preço total é
    quantidade × preço da unidade e nunca muda por causa de vendas posteriores.
    Ao registrar a compra, a quantidade é somada ao estoque do produto
    (ver servicos.registrar_compra)."""
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fornecedores')
    # SET_NULL: excluir o produto NÃO apaga o histórico financeiro das compras.
    produto = models.ForeignKey(
        Produto, on_delete=models.SET_NULL, null=True, blank=True, related_name='compras',
        verbose_name='Produto comprado'
    )
    nome_produto = models.CharField(max_length=120, blank=True, editable=False)
    nome_fornecedor = models.CharField('Nome do fornecedor', max_length=120)
    quantidade = models.DecimalField('Quantidade comprada', default=0, validators=[MinValueValidator(0)], **QTD)
    preco_unidade = models.DecimalField('Preço da unidade', max_digits=10, decimal_places=2,
                                        validators=[MinValueValidator(0)])
    preco_total = models.DecimalField('Preço total pago', max_digits=10, decimal_places=2, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.nome_produto or self.produto} - {self.nome_fornecedor}'

    def save(self, *args, **kwargs):
        if self.produto_id and not self.nome_produto:
            self.nome_produto = self.produto.nome
        # Sempre recalculado no servidor, com a quantidade DESTA compra.
        self.preco_total = (Decimal(str(self.quantidade)) * Decimal(str(self.preco_unidade))).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)


class Reserva(models.Model):
    """Reserva de mesa. Isolada por restaurante (usuario) — cada restaurante só
    vê e gerencia suas próprias reservas."""

    PAGAMENTO_CHOICES = [
        ('dinheiro', 'Dinheiro'),
        ('debito', 'Cartão de Débito'),
        ('credito', 'Cartão de Crédito'),
        ('pix', 'Pix'),
    ]

    VALOR_POR_PESSOA = 50

    # Cada reserva ocupa a mesa por este tempo (19:00 e 19:30 na mesma mesa conflitam).
    DURACAO_HORAS = 2

    usuario = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='reservas', null=True, blank=True
    )
    mesa = models.PositiveSmallIntegerField(choices=MESA_CHOICES)
    cliente_nome = models.CharField(max_length=120)
    cliente_telefone = models.CharField(max_length=20, blank=True)
    data = models.DateField()
    horario = models.TimeField(help_text='Horário de funcionamento: 18:00 até 01:00')
    quantidade_pessoas = models.PositiveSmallIntegerField(
        'Quantidade de pessoas', default=1, validators=[MinValueValidator(1)]
    )
    tipo_pagamento = models.CharField(
        'Tipo de pagamento', max_length=10, choices=PAGAMENTO_CHOICES, default='dinheiro'
    )
    pago = models.BooleanField('Pagamento confirmado', default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['data', 'horario', 'mesa']
        constraints = [
            models.UniqueConstraint(
                fields=['usuario', 'mesa', 'data', 'horario'], name='reserva_unica_por_usuario'
            ),
            # Em SQL, NULL é sempre "diferente" de NULL; esta regra cobre reservas sem usuário.
            models.UniqueConstraint(
                fields=['mesa', 'data', 'horario'], condition=Q(usuario__isnull=True),
                name='reserva_unica_sem_usuario',
            ),
        ]

    def __str__(self):
        return f'Mesa {self.mesa} - {self.data} {self.horario} - {self.cliente_nome}'

    @staticmethod
    def inicio_efetivo(data, horario):
        """Momento real da reserva. O campo `data` é o DIA DO EXPEDIENTE: uma
        reserva às 00:30 do expediente de sexta acontece na madrugada de sábado."""
        from datetime import datetime, time, timedelta
        momento = datetime.combine(data, horario)
        if horario <= time(1, 0):
            momento += timedelta(days=1)
        return timezone.make_aware(momento)

    @property
    def valor(self):
        """Valor da reserva: quantidade de pessoas × R$ 50,00 por pessoa."""
        return self.quantidade_pessoas * self.VALOR_POR_PESSOA


class DespesaFixa(models.Model):
    """Contas fixas mensais do restaurante: aluguel, água, energia, outros."""

    MESES_CHOICES = [
        (1, 'Janeiro'), (2, 'Fevereiro'), (3, 'Março'), (4, 'Abril'),
        (5, 'Maio'), (6, 'Junho'), (7, 'Julho'), (8, 'Agosto'),
        (9, 'Setembro'), (10, 'Outubro'), (11, 'Novembro'), (12, 'Dezembro'),
    ]
    TIPO_CHOICES = [
        ('aluguel', 'Aluguel'),
        ('agua', 'Água'),
        ('energia', 'Energia'),
        ('outros', 'Outros'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='despesas_fixas')
    tipo = models.CharField('Tipo', max_length=10, choices=TIPO_CHOICES)
    valor = models.DecimalField('Valor', max_digits=10, decimal_places=2,
                                validators=[MinValueValidator(Decimal('0.01'))])
    mes = models.PositiveSmallIntegerField('Mês', choices=MESES_CHOICES)
    ano = models.PositiveSmallIntegerField('Ano', validators=[MinValueValidator(2000), MaxValueValidator(2100)])
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-ano', '-mes', 'tipo']
        verbose_name = 'Despesa Fixa'
        verbose_name_plural = 'Despesas Fixas'

    def __str__(self):
        return f'{self.get_tipo_display()} - {self.get_mes_display()}/{self.ano} - R$ {self.valor}'


# Faixas iniciais (°C) de uma zona criada automaticamente pelo primeiro envio do sensor.
FAIXA_PADRAO_REFRIGERADO = (Decimal('0'), Decimal('8'))
FAIXA_PADRAO_FREEZER = (Decimal('-25'), Decimal('-15'))


class ZonaTemperatura(models.Model):
    ZONA_CHOICES = [
        ('A', 'Zona A'),
        ('B', 'Zona B'),
        ('C', 'Zona C'),
        ('D', 'Zona D'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='zonas_temperatura')
    zona = models.CharField(max_length=1, choices=ZONA_CHOICES)
    temp_minima = models.DecimalField(max_digits=5, decimal_places=2)
    temp_maxima = models.DecimalField(max_digits=5, decimal_places=2)
    temp_atual = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('usuario', 'zona')
        ordering = ['zona']
        verbose_name = 'Zona de Temperatura'
        verbose_name_plural = 'Zonas de Temperatura'

    def __str__(self):
        return f"{self.usuario} - Zona {self.zona}"

    @property
    def em_alerta(self):
        """True se a temperatura atual estiver fora da faixa segura."""
        if self.temp_atual is None:
            return False
        return self.temp_atual > self.temp_maxima or self.temp_atual < self.temp_minima

    @property
    def status_temperatura(self):
        """'sem-leitura', 'fora' (acima da máxima ou abaixo da mínima) ou 'ok' (entre elas)."""
        if self.temp_atual is None:
            return 'sem-leitura'
        return 'fora' if self.em_alerta else 'ok'


class SensorESP32(models.Model):
    TIPO_CHOICES = [
        ('DHT22', 'DHT22 (temperatura e umidade)'),
        ('DS18B20', 'DS18B20 (temperatura - freezer)'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sensores')
    nome = models.CharField(max_length=50, help_text="Ex: ESP32 - Câmara fria 1")
    tipo_sensor = models.CharField(max_length=10, choices=TIPO_CHOICES, default='DHT22')
    zona = models.CharField(
        max_length=1,
        choices=ZonaTemperatura.ZONA_CHOICES,
        null=True,
        blank=True,
        help_text=(
            "Trava este sensor a UMA zona específica (ex: Zona A pro DS18B20 do freezer). "
            "Se o token for usado pra enviar uma zona diferente desta, o envio é recusado. "
            "Deixe em branco se este token enviar mais de uma zona."
        ),
    )
    token = models.CharField(max_length=64, unique=True, default=secrets.token_hex, editable=False)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Sensor ESP32'
        verbose_name_plural = 'Sensores ESP32'

    def __str__(self):
        zona_str = f" - Zona {self.zona}" if self.zona else ""
        return f"{self.nome} ({self.tipo_sensor}{zona_str}) - {self.usuario}"


class Notificacao(models.Model):
    """Alerta gerado automaticamente para o usuário: temperatura fora da faixa,
    produto perto de vencer ou estoque baixo."""

    TIPO_CHOICES = [
        ('temperatura', 'Temperatura'),
        ('validade', 'Produto vencendo'),
        ('estoque', 'Estoque baixo'),
        ('excesso', 'Estoque excedente'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notificacoes')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    mensagem = models.CharField(max_length=255)

    # Ligações opcionais, para saber exatamente qual zona/produto gerou o alerta
    # e conseguir resolvê-lo automaticamente quando o problema passar.
    zona = models.ForeignKey(
        ZonaTemperatura, on_delete=models.CASCADE, null=True, blank=True, related_name='notificacoes'
    )
    produto = models.ForeignKey(
        Produto, on_delete=models.CASCADE, null=True, blank=True, related_name='notificacoes'
    )

    criada_em = models.DateTimeField(auto_now_add=True)
    resolvida = models.BooleanField(default=False)

    class Meta:
        ordering = ['-criada_em']
        verbose_name = 'Notificação'
        verbose_name_plural = 'Notificações'

    def __str__(self):
        return f'[{self.get_tipo_display()}] {self.mensagem}'


class MovimentacaoEstoque(models.Model):
    """Histórico de toda entrada/saída/ajuste de quantidade de um produto no
    estoque. Criado automaticamente em compras (Fornecedor) e também pode ser
    lançado manualmente (uso na cozinha, perda, ajuste de contagem)."""

    TIPO_CHOICES = [
        ('entrada', 'Entrada (compra)'),
        ('saida', 'Saída (uso/consumo)'),
        ('perda', 'Perda/Descarte'),
        ('ajuste', 'Ajuste de contagem'),
        ('estorno', 'Estorno (pedido cancelado)'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='movimentacoes_estoque')
    # SET_NULL: o histórico fica mesmo que o produto seja excluído.
    produto = models.ForeignKey(
        Produto, on_delete=models.SET_NULL, null=True, blank=True, related_name='movimentacoes'
    )
    nome_produto = models.CharField(max_length=120, blank=True, editable=False)
    pedido = models.ForeignKey(
        'Pedido', on_delete=models.SET_NULL, null=True, blank=True, related_name='movimentacoes'
    )
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    quantidade = models.DecimalField(
        validators=[MinValueValidator(0)],
        help_text='Quantidade movimentada (sempre um valor positivo).', **QTD
    )
    quantidade_resultante = models.DecimalField(
        'Quantidade em estoque após o movimento', editable=False, **QTD
    )
    observacao = models.CharField(max_length=200, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'Movimentação de Estoque'
        verbose_name_plural = 'Movimentações de Estoque'

    def __str__(self):
        return f'{self.get_tipo_display()} - {self.nome_produto or self.produto} ({self.quantidade})'

    def save(self, *args, **kwargs):
        if self.produto_id and not self.nome_produto:
            self.nome_produto = self.produto.nome
        super().save(*args, **kwargs)

    @property
    def unidade_display(self):
        return self.produto.get_unidade_display() if self.produto_id else ''


class Prato(models.Model):
    """Item do cardápio. A ficha técnica (ItemFichaTecnica) define quanto de
    cada produto do estoque é consumido ao vender uma unidade do prato."""

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pratos')
    nome = models.CharField(max_length=120)
    preco_venda = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nome']

    def __str__(self):
        return self.nome

    def pode_ser_vendido(self):
        """True se houver estoque suficiente de TODOS os insumos da ficha
        técnica para produzir mais uma unidade deste prato. Prato SEM ficha
        técnica não pode ser vendido (nada baixaria do estoque).
        Use prefetch_related('itens_ficha_tecnica__produto') nas listagens."""
        itens = list(self.itens_ficha_tecnica.all())
        if not itens:
            return False
        return all(item.produto.quantidade >= item.quantidade_usada for item in itens)


class ItemFichaTecnica(models.Model):
    """Um insumo (Produto) e a quantidade dele consumida ao preparar uma
    unidade de um Prato."""

    prato = models.ForeignKey(Prato, on_delete=models.CASCADE, related_name='itens_ficha_tecnica')
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE, related_name='usado_em_pratos')
    quantidade_usada = models.DecimalField(
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Quantidade deste produto consumida ao preparar 1 unidade do prato.', **QTD
    )

    class Meta:
        unique_together = ('prato', 'produto')
        verbose_name = 'Item da Ficha Técnica'
        verbose_name_plural = 'Itens da Ficha Técnica'

    def __str__(self):
        return f'{self.prato.nome}: {self.quantidade_usada} {self.produto.get_unidade_display()} de {self.produto.nome}'


class Pedido(models.Model):
    """Pedido feito pelo CLIENTE na mesa (ou venda de balcão registrada pelo
    restaurante, com mesa vazia). Isolado por restaurante (usuario).

    Fluxo: recebido (aguardando o restaurante) → confirmado (estoque baixado)
    → entregue. Pode ser cancelado antes de entregue; se já estava confirmado,
    o estoque é devolvido. Só pedidos ENTREGUES contam como receita."""

    STATUS_CHOICES = [
        ('recebido', 'Recebido'),
        ('confirmado', 'Confirmado'),
        ('entregue', 'Entregue'),
        ('cancelado', 'Cancelado'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pedidos')
    mesa = models.PositiveSmallIntegerField(choices=MESA_CHOICES, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='recebido')
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'Pedido #{self.pk} - {self.mesa_display}'

    @property
    def mesa_display(self):
        return f'Mesa {self.mesa}' if self.mesa else 'Balcão'

    @property
    def total(self):
        return sum((item.subtotal for item in self.itens.all()), start=Decimal('0'))


class ItemPedido(models.Model):
    """Um prato e a quantidade pedida. Guarda o nome e o preço do momento do
    pedido, para o histórico não mudar se o prato for editado ou excluído."""

    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='itens')
    prato = models.ForeignKey(Prato, on_delete=models.SET_NULL, null=True, blank=True, related_name='itens_pedido')
    nome_prato = models.CharField(max_length=120)
    preco_unitario = models.DecimalField(max_digits=8, decimal_places=2)
    quantidade = models.PositiveSmallIntegerField(default=1)

    def __str__(self):
        return f'{self.quantidade}x {self.nome_prato}'

    @property
    def subtotal(self):
        return self.preco_unitario * self.quantidade
