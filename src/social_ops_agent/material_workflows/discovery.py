"""Link discovery; anonymous by default or using an explicitly selected window."""


def discover(context, item):
    return context.call(
        'discover_public_materials',
        {'options': {**context.options, 'start_url': str(item)}},
    )
