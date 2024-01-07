from cornice.validators import colander_body_validator


def bound_schema_validator(request, **kwargs):
    schema = kwargs["schema"]
    kwargs["schema"] = schema.bind(request=request)
    return colander_body_validator(request, **kwargs)
