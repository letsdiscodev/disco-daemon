from colander import Schema, SchemaNode, String

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
class CreateVolumeSchema(Schema):
    name = SchemaNode(String(), preparer=PREPARERS)
