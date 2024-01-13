from colander import Integer, Schema, SchemaNode

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
class AddPublishedPortSchema(Schema):
    hostPort = SchemaNode(Integer(), preparer=PREPARERS)
    containerPort = SchemaNode(Integer(), preparer=PREPARERS)
