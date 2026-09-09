from datetime import time
from django import forms
from django.forms import inlineformset_factory
from .models import (
    Produto, Reserva, Fornecedor, ItemEstoque, DespesaFixa, MovimentacaoEstoque,
    Prato, ItemFichaTecnica,
)

HORA_ABERTURA = time(18, 0)   # 18:00
HORA_FECHAMENTO = time(1, 0)  # 01:00 (do dia seguinte)


class ItemEstoqueForm(forms.ModelForm):
    class Meta:
        model = ItemEstoque
        fields = ['nome', 'quantidade', 'unidade']


class ProdutoForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = [
            'nome', 'categoria', 'quantidade', 'quantidade_minima',
            'quantidade_maxima', 'unidade', 'validade',
        ]
        widgets = {
            'validade': forms.DateInput(attrs={'type': 'date'}),
        }


class MovimentacaoEstoqueForm(forms.ModelForm):
    """Lançamento manual de saída, perda ou ajuste de contagem de um produto.
    A entrada por compra é sempre feita pela tela de Fornecedores."""

    class Meta:
        model = MovimentacaoEstoque
        fields = ['produto', 'tipo', 'quantidade', 'observacao']

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Lançamento manual não permite registrar "entrada" (isso é feito
        # automaticamente ao cadastrar uma compra em Fornecedores).
        self.fields['tipo'].choices = [
            c for c in MovimentacaoEstoque.TIPO_CHOICES if c[0] != 'entrada'
        ]
        if usuario is not None:
            self.fields['produto'].queryset = Produto.objects.filter(usuario=usuario)

    def clean(self):
        cleaned_data = super().clean()
        produto = cleaned_data.get('produto')
        quantidade = cleaned_data.get('quantidade')
        tipo = cleaned_data.get('tipo')

        if produto and quantidade and tipo in ('saida', 'perda'):
            if quantidade > produto.quantidade:
                raise forms.ValidationError(
                    f'Quantidade maior que o estoque atual de {produto.nome} '
                    f'({produto.quantidade} {produto.get_unidade_display()}).'
                )
        return cleaned_data


class FornecedorForm(forms.ModelForm):
    class Meta:
        model = Fornecedor
        fields = ['produto', 'nome_fornecedor', 'preco_unidade']

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        if usuario is not None:
            # Só mostra produtos do próprio usuário logado (isolamento entre usuários)
            self.fields['produto'].queryset = Produto.objects.filter(usuario=usuario)


class DespesaFixaForm(forms.ModelForm):
    class Meta:
        model = DespesaFixa
        fields = ['tipo', 'valor', 'mes', 'ano']


class ReservaForm(forms.ModelForm):
    class Meta:
        model = Reserva
        fields = [
            'mesa', 'cliente_nome', 'cliente_telefone', 'data', 'horario',
            'quantidade_pessoas', 'tipo_pagamento',
        ]
        widgets = {
            'data': forms.DateInput(attrs={'type': 'date'}),
            'horario': forms.TimeInput(attrs={'type': 'time'}),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario

    def clean_horario(self):
        horario = self.cleaned_data['horario']
        # Funcionamento das 18h às 01h (cruza a meia-noite)
        dentro_do_horario = horario >= HORA_ABERTURA or horario <= HORA_FECHAMENTO
        if not dentro_do_horario:
            raise forms.ValidationError(
                'Horário fora do funcionamento (das 18:00 às 01:00).'
            )
        return horario

    def clean(self):
        cleaned_data = super().clean()
        mesa = cleaned_data.get('mesa')
        data = cleaned_data.get('data')
        horario = cleaned_data.get('horario')

        if self.usuario and mesa and data and horario:
            conflito = Reserva.objects.filter(
                usuario=self.usuario, mesa=mesa, data=data, horario=horario
            )
            if self.instance.pk:
                conflito = conflito.exclude(pk=self.instance.pk)
            if conflito.exists():
                raise forms.ValidationError(
                    f'A Mesa {mesa} já está reservada para {data} às {horario}. '
                    'Escolha outro horário ou outra mesa.'
                )
        return cleaned_data

class PratoForm(forms.ModelForm):
    class Meta:
        model = Prato
        fields = ['nome', 'preco_venda', 'ativo']


class ItemFichaTecnicaForm(forms.ModelForm):
    class Meta:
        model = ItemFichaTecnica
        fields = ['produto', 'quantidade_usada']

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        if usuario is not None:
            self.fields['produto'].queryset = Produto.objects.filter(usuario=usuario)


# Formset para cadastrar/editar os insumos de um prato junto com o próprio prato.
ItemFichaTecnicaFormSet = inlineformset_factory(
    Prato,
    ItemFichaTecnica,
    form=ItemFichaTecnicaForm,
    extra=3,
    can_delete=True,
)
