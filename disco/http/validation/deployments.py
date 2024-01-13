from colander import Schema, SchemaNode, String

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
class CreateDeploymentSchema(Schema):
    image = SchemaNode(String(), preparer=PREPARERS)
