from datetime import time

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import (
    Produto, Reserva, Fornecedor, DespesaFixa, MovimentacaoEstoque,
    Prato, ItemFichaTecnica,
)

HORA_ABERTURA = time(18, 0)   # 18:00
HORA_FECHAMENTO = time(1, 0)  # 01:00 (do dia seguinte)


class ProdutoForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = [
            'nome', 'categoria', 'quantidade', 'quantidade_minima',
            'quantidade_maxima', 'unidade', 'validade',
        ]
        widgets = {
            'validade': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario

    def clean_nome(self):
        nome = self.cleaned_data['nome'].strip()
        if self.usuario is not None:
            repetido = Produto.objects.filter(usuario=self.usuario, nome__iexact=nome)
            if self.instance.pk:
                repetido = repetido.exclude(pk=self.instance.pk)
            if repetido.exists():
                raise forms.ValidationError('Você já tem um produto com esse nome.')
        return nome

    def clean_validade(self):
        validade = self.cleaned_data['validade']
        # Em edição, um produto que já estava vencido pode continuar com a mesma data.
        data_inalterada = self.instance.pk and self.instance.validade == validade
        if validade < timezone.localdate() and not data_inalterada:
            raise forms.ValidationError('A validade não pode estar no passado.')
        return validade


class MovimentacaoEstoqueForm(forms.ModelForm):
    """Lançamento manual de saída, perda ou ajuste de contagem de um produto.
    A entrada por compra é sempre feita pela tela de Fornecedores."""

    class Meta:
        model = MovimentacaoEstoque
        fields = ['produto', 'tipo', 'quantidade', 'observacao']

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Lançamento manual não permite registrar "entrada" (isso é feito
        # automaticamente ao cadastrar uma compra em Fornecedores) nem estorno.
        self.fields['tipo'].choices = [
            c for c in MovimentacaoEstoque.TIPO_CHOICES if c[0] in ('saida', 'perda', 'ajuste')
        ]
        if usuario is not None:
            self.fields['produto'].queryset = Produto.objects.filter(usuario=usuario)

    def clean(self):
        cleaned_data = super().clean()
        produto = cleaned_data.get('produto')
        quantidade = cleaned_data.get('quantidade')
        tipo = cleaned_data.get('tipo')

        if quantidade is not None and tipo in ('saida', 'perda') and quantidade <= 0:
            raise forms.ValidationError('Informe uma quantidade maior que zero.')
        if produto and quantidade and tipo in ('saida', 'perda'):
            if quantidade > produto.quantidade:
                raise forms.ValidationError(
                    f'Quantidade maior que o estoque atual de {produto.nome} '
                    f'({produto.quantidade.normalize():f} {produto.get_unidade_display()}).'
                )
        return cleaned_data


class FornecedorForm(forms.ModelForm):
    """Nova compra: produto, quantidade comprada e preço da unidade. Ao editar,
    produto e quantidade ficam travados (já entraram no estoque e no histórico);
    só dá para corrigir o nome do fornecedor e o preço da unidade."""

    class Meta:
        model = Fornecedor
        fields = ['produto', 'nome_fornecedor', 'quantidade', 'preco_unidade']

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        if usuario is not None:
            # Só mostra produtos do próprio usuário logado (isolamento entre usuários)
            self.fields['produto'].queryset = Produto.objects.filter(usuario=usuario)
        self.fields['produto'].required = True
        if self.instance.pk:
            for campo in ('produto', 'quantidade'):
                self.fields[campo].disabled = True

    def clean_quantidade(self):
        quantidade = self.cleaned_data['quantidade']
        if quantidade is not None and quantidade <= 0:
            raise forms.ValidationError('Informe uma quantidade maior que zero.')
        return quantidade


class DespesaFixaForm(forms.ModelForm):
    class Meta:
        model = DespesaFixa
        fields = ['tipo', 'valor', 'mes', 'ano']

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario

    def clean(self):
        cleaned_data = super().clean()
        tipo, mes, ano = (cleaned_data.get(k) for k in ('tipo', 'mes', 'ano'))
        # "Outros" pode ter vários lançamentos no mês; aluguel/água/energia, só um.
        if self.usuario and tipo and mes and ano and tipo != 'outros':
            repetida = DespesaFixa.objects.filter(usuario=self.usuario, tipo=tipo, mes=mes, ano=ano)
            if self.instance.pk:
                repetida = repetida.exclude(pk=self.instance.pk)
            if repetida.exists():
                raise forms.ValidationError(
                    f'Já existe uma despesa de {dict(DespesaFixa.TIPO_CHOICES)[tipo]} '
                    f'em {mes:02d}/{ano}. Edite a existente.'
                )
        return cleaned_data


class ReservaForm(forms.ModelForm):
    class Meta:
        model = Reserva
        fields = [
            'mesa', 'cliente_nome', 'cliente_telefone', 'data', 'horario',
            'quantidade_pessoas', 'tipo_pagamento',
        ]
        widgets = {
            'data': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'horario': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.fields['quantidade_pessoas'].min_value = 1
        self.fields['quantidade_pessoas'].widget.attrs['min'] = 1

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

        if data and horario:
            # `data` é o dia do expediente: 00:30 da reserva de sexta é madrugada de sábado.
            inicio = Reserva.inicio_efetivo(data, horario)
            if inicio < timezone.now() and not self.instance.pk:
                raise forms.ValidationError('Não é possível reservar para um horário que já passou.')

        if self.usuario and mesa and data and horario:
            from datetime import timedelta
            duracao = timedelta(hours=Reserva.DURACAO_HORAS)
            inicio = Reserva.inicio_efetivo(data, horario)
            outras = Reserva.objects.filter(usuario=self.usuario, mesa=mesa, data=data)
            if self.instance.pk:
                outras = outras.exclude(pk=self.instance.pk)
            for outra in outras:
                if abs(Reserva.inicio_efetivo(outra.data, outra.horario) - inicio) < duracao:
                    raise forms.ValidationError(
                        f'A Mesa {mesa} já tem reserva às {outra.horario:%H:%M} em {data:%d/%m/%Y}. '
                        f'Cada reserva ocupa a mesa por {Reserva.DURACAO_HORAS} horas; '
                        'escolha outro horário ou outra mesa.'
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


class CadastroForm(UserCreationForm):
    """Formulário de cadastro de novo restaurante (usuário + senha).
    Mensagens de erro em português e checagem de usuário duplicado
    ignorando maiúsculas/minúsculas (Yas == yas)."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username',)
        labels = {'username': 'Usuário'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Usuário'
        self.fields['username'].help_text = 'Letras, números e @/./+/-/_ (máx. 150).'
        self.fields['username'].widget.attrs.update({'autofocus': True, 'autocomplete': 'username'})
        self.fields['password1'].label = 'Senha'
        self.fields['password1'].help_text = 'Mínimo de 8 caracteres, não pode ser só números nem muito comum.'
        self.fields['password1'].widget.attrs['autocomplete'] = 'new-password'
        self.fields['password2'].label = 'Confirmar senha'
        self.fields['password2'].help_text = 'Digite a mesma senha novamente.'
        self.fields['password2'].widget.attrs['autocomplete'] = 'new-password'
        self.error_messages['password_mismatch'] = 'As duas senhas não são iguais.'
        self.fields['username'].error_messages['unique'] = 'Já existe um usuário com esse nome.'
        self.fields['username'].error_messages['required'] = 'Informe o nome de usuário.'
        self.fields['password1'].error_messages['required'] = 'Informe a senha.'
        self.fields['password2'].error_messages['required'] = 'Confirme a senha.'

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('Já existe um usuário com esse nome.')
        return username
