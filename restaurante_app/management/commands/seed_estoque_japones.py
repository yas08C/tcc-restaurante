from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from restaurante_app.models import Produto


# Estimativas de consumo médio diário para um restaurante japonês de porte
# médio (~80-120 pratos/dia). quantidade_minima = 3 dias de consumo médio
# (margem de segurança até chegar novo pedido do fornecedor). quantidade_maxima
# evita compra/estoque excessivo, considerando também a perecibilidade de cada
# item (itens perecíveis como salmão, tomate e laranja têm margem mais curta;
# itens não perecíveis como arroz, sal e açúcar toleram um estoque maior).
PRODUTOS_ESTIMATIVA = [
    # nome, categoria, unidade, validade_dias, quantidade_minima, quantidade_maxima
    ('Salmão', 'carnes', 'kg', 3, 30, 45),
    ('Alga (nori)', 'graos_massas', 'kg', 180, 2, 3),
    ('Arroz', 'graos_massas', 'kg', 365, 54, 90),
    ('Shoyo', 'outros', 'l', 365, 10, 20),
    ('Macarrão', 'graos_massas', 'kg', 365, 18, 30),
    ('Tomate', 'hortifruti', 'kg', 7, 8, 12),
    ('Laranja', 'hortifruti', 'kg', 10, 8, 12),
    ('Pimentão', 'hortifruti', 'kg', 7, 5, 8),
    ('Sal', 'outros', 'kg', 730, 2, 5),
    ('Açúcar', 'outros', 'kg', 730, 5, 10),
]


class Command(BaseCommand):
    help = (
        'Cria (ou atualiza) os produtos de um restaurante japonês com '
        'quantidade mínima e máxima estimadas para 3 dias de consumo médio. '
        'Uso: python manage.py seed_estoque_japones <username>'
    )

    def add_arguments(self, parser):
        parser.add_argument('username', type=str, help='Usuário dono do estoque (login do sistema).')
        parser.add_argument(
            '--quantidade-inicial', type=int, default=0,
            help='Quantidade inicial a cadastrar para cada produto novo (padrão: 0).'
        )

    def handle(self, *args, **options):
        username = options['username']
        quantidade_inicial = options['quantidade_inicial']

        try:
            usuario = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'Usuário "{username}" não encontrado.')

        criados, atualizados = 0, 0
        hoje = date.today()

        for nome, categoria, unidade, validade_dias, qtd_min, qtd_max in PRODUTOS_ESTIMATIVA:
            produto, criado = Produto.objects.get_or_create(
                usuario=usuario,
                nome=nome,
                defaults={
                    'categoria': categoria,
                    'unidade': unidade,
                    'quantidade': quantidade_inicial,
                    'quantidade_minima': qtd_min,
                    'quantidade_maxima': qtd_max,
                    'validade': hoje + timedelta(days=validade_dias),
                },
            )
            if criado:
                criados += 1
            else:
                produto.quantidade_minima = qtd_min
                produto.quantidade_maxima = qtd_max
                produto.save(update_fields=['quantidade_minima', 'quantidade_maxima'])
                atualizados += 1

        self.stdout.write(self.style.SUCCESS(
            f'Concluído: {criados} produto(s) criado(s), {atualizados} atualizado(s) '
            f'com mínimo/máximo para o usuário "{username}".'
        ))
