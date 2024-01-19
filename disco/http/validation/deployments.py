from colander import Schema, SchemaNode, String

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
class CreateDeploymentSchema(Schema):
    discoConfig = SchemaNode(String(), preparer=PREPARERS, missing=None)
    commit = SchemaNode(String(), preparer=PREPARERS, missing=None)
