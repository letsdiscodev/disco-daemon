from colander import Schema, SchemaNode, String

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
# TODO should not contain characters used in --mount
class CreateVolumeSchema(Schema):
    name = SchemaNode(String(), preparer=PREPARERS)


class AttachVolumeSchema(Schema):
    destination = SchemaNode(String(), preparer=PREPARERS)
