"""Public discovery workflow; authenticated browser tools remain separate."""


def discover(context, item):
    return context.call(
        'discover_public_materials',
        {'options': {**context.options, 'start_url': str(item)}},
    )
