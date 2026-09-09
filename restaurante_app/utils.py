from datetime import timedelta

from django.utils import timezone

from .models import Produto, Notificacao


def verificar_alertas_estoque(usuario):
    """Confere todos os produtos do usuário e cria/resolve notificações de
    'perto de vencer' (3 dias ou menos) e 'estoque baixo'. Chamado sempre que
    o banner de notificações é consultado (view notificacoes_ativas), então
    não precisa de tarefa agendada em segundo plano."""
    hoje = timezone.localdate()
    limite_validade = hoje + timedelta(days=3)

    for produto in Produto.objects.filter(usuario=usuario):
        # --- Validade ---
        perto_de_vencer = hoje <= produto.validade <= limite_validade
        if perto_de_vencer:
            dias = (produto.validade - hoje).days
            texto_dias = 'hoje' if dias == 0 else f'em {dias} dia(s)'
            # update_or_create: se a notificação já existir, atualiza a mensagem
            # com a contagem de dias recalculada; se não existir, cria. Assim o
            # texto acompanha a data atual em vez de ficar travado no dia da criação.
            Notificacao.objects.update_or_create(
                usuario=usuario, tipo='validade', produto=produto, resolvida=False,
                defaults={'mensagem': f'{produto.nome} vence {texto_dias}!'}
            )
        else:
            Notificacao.objects.filter(
                usuario=usuario, tipo='validade', produto=produto, resolvida=False
            ).update(resolvida=True)

        # --- Estoque baixo ---
        estoque_baixo = produto.quantidade <= produto.quantidade_minima
        if estoque_baixo:
            ja_existe = Notificacao.objects.filter(
                usuario=usuario, tipo='estoque', produto=produto, resolvida=False
            ).exists()
            if not ja_existe:
                Notificacao.objects.create(
                    usuario=usuario, tipo='estoque', produto=produto,
                    mensagem=f'{produto.nome} está com estoque baixo '
                             f'({produto.quantidade} {produto.get_unidade_display()})!'
                )
        else:
            Notificacao.objects.filter(
                usuario=usuario, tipo='estoque', produto=produto, resolvida=False
            ).update(resolvida=True)

        # --- Estoque excedente ---
        if produto.estoque_excedente:
            ja_existe = Notificacao.objects.filter(
                usuario=usuario, tipo='excesso', produto=produto, resolvida=False
            ).exists()
            if not ja_existe:
                Notificacao.objects.create(
                    usuario=usuario, tipo='excesso', produto=produto,
                    mensagem=f'{produto.nome} está com estoque acima do recomendado '
                             f'({produto.quantidade} {produto.get_unidade_display()}, '
                             f'máximo {produto.quantidade_maxima})!'
                )
        else:
            Notificacao.objects.filter(
                usuario=usuario, tipo='excesso', produto=produto, resolvida=False
            ).update(resolvida=True)