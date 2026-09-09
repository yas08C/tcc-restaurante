import secrets
from django.db import models
from django.contrib.auth.models import User

MESA_CHOICES = [(i, f'Mesa {i}') for i in range(1, 16)]


class ItemEstoque(models.Model):
    UNIDADE_CHOICES = [
        ('kg', 'Quilograma (kg)'),
        ('l', 'Litro (L)'),
        ('un', 'Unidade'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='itens_estoque')
    nome = models.CharField(max_length=100)
    quantidade = models.DecimalField(max_digits=10, decimal_places=2)
    unidade = models.CharField(max_length=2, choices=UNIDADE_CHOICES, default='un')

    def __str__(self):
        return f'{self.nome} ({self.quantidade} {self.get_unidade_display()})'


class Produto(models.Model):
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
    quantidade = models.PositiveIntegerField(default=0)
    quantidade_minima = models.PositiveIntegerField(
        'Quantidade mínima', default=5,
        help_text='Abaixo desse valor, um alerta de estoque baixo é disparado.'
    )
    quantidade_maxima = models.PositiveIntegerField(
        'Quantidade máxima', null=True, blank=True,
        help_text='Acima desse valor, indica estoque excessivo (compra além do necessário). Opcional.'
    )
    unidade = models.CharField('Unidade', max_length=2, choices=UNIDADE_CHOICES, default='un')
    validade = models.DateField()
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['validade']

    def __str__(self):
        return self.nome

    @property
    def vencido(self):
        from datetime import date
        return self.validade < date.today()

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
        ultima_compra = self.compras.order_by('-criado_em').first()
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
    """Registro de compra feita a um fornecedor. Cada registro pertence a um usuário."""
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fornecedores')
    produto = models.ForeignKey(
        Produto, on_delete=models.CASCADE, related_name='compras',
        verbose_name='Produto comprado'
    )
    nome_fornecedor = models.CharField('Nome do fornecedor', max_length=120)
    preco_unidade = models.DecimalField('Preço da unidade', max_digits=10, decimal_places=2)
    preco_total = models.DecimalField('Preço total pago', max_digits=10, decimal_places=2, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.produto.nome} - {self.nome_fornecedor}'

    def save(self, *args, **kwargs):
        # Preço total = quantidade que já está cadastrada no estoque do produto × preço da unidade.
        # Sempre recalculado no servidor, nunca confiando no que veio do formulário.
        self.preco_total = self.produto.quantidade * self.preco_unidade
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

    usuario = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='reservas', null=True, blank=True
    )
    mesa = models.PositiveSmallIntegerField(choices=MESA_CHOICES)
    cliente_nome = models.CharField(max_length=120)
    cliente_telefone = models.CharField(max_length=20, blank=True)
    data = models.DateField()
    horario = models.TimeField(help_text='Horário de funcionamento: 18:00 até 01:00')
    quantidade_pessoas = models.PositiveSmallIntegerField('Quantidade de pessoas', default=1)
    tipo_pagamento = models.CharField(
        'Tipo de pagamento', max_length=10, choices=PAGAMENTO_CHOICES, default='dinheiro'
    )
    pago = models.BooleanField('Pagamento confirmado', default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['data', 'horario', 'mesa']
        unique_together = ('usuario', 'mesa', 'data', 'horario')

    def __str__(self):
        return f'Mesa {self.mesa} - {self.data} {self.horario} - {self.cliente_nome}'

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
    valor = models.DecimalField('Valor', max_digits=10, decimal_places=2)
    mes = models.PositiveSmallIntegerField('Mês', choices=MESES_CHOICES)
    ano = models.PositiveSmallIntegerField('Ano')
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-ano', '-mes', 'tipo']
        verbose_name = 'Despesa Fixa'
        verbose_name_plural = 'Despesas Fixas'

    def __str__(self):
        return f'{self.get_tipo_display()} - {self.get_mes_display()}/{self.ano} - R$ {self.valor}'


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


class SensorESP32(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sensores')
    nome = models.CharField(max_length=50, help_text="Ex: ESP32 - Câmara fria 1")
    token = models.CharField(max_length=64, unique=True, default=secrets.token_hex, editable=False)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Sensor ESP32'
        verbose_name_plural = 'Sensores ESP32'

    def __str__(self):
        return f"{self.nome} ({self.usuario})"


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
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='movimentacoes_estoque')
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE, related_name='movimentacoes')
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    quantidade = models.PositiveIntegerField(help_text='Quantidade movimentada (sempre um valor positivo).')
    quantidade_resultante = models.PositiveIntegerField(
        'Quantidade em estoque após o movimento', editable=False
    )
    observacao = models.CharField(max_length=200, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'Movimentação de Estoque'
        verbose_name_plural = 'Movimentações de Estoque'

    def __str__(self):
        return f'{self.get_tipo_display()} - {self.produto.nome} ({self.quantidade})'


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
        técnica para produzir mais uma unidade deste prato."""
        for item in self.itens_ficha_tecnica.select_related('produto'):
            if item.produto.quantidade < item.quantidade_usada:
                return False
        return True


class ItemFichaTecnica(models.Model):
    """Um insumo (Produto) e a quantidade dele consumida ao preparar uma
    unidade de um Prato."""

    prato = models.ForeignKey(Prato, on_delete=models.CASCADE, related_name='itens_ficha_tecnica')
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE, related_name='usado_em_pratos')
    quantidade_usada = models.PositiveIntegerField(
        help_text='Quantidade deste produto consumida ao preparar 1 unidade do prato.'
    )

    class Meta:
        unique_together = ('prato', 'produto')
        verbose_name = 'Item da Ficha Técnica'
        verbose_name_plural = 'Itens da Ficha Técnica'

    def __str__(self):
        return f'{self.prato.nome}: {self.quantidade_usada} {self.produto.get_unidade_display()} de {self.produto.nome}'
