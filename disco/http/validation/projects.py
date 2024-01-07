from colander import Schema, SchemaNode, String

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
class CreateProjectSchema(Schema):
    name = SchemaNode(String(), preparer=PREPARERS)
    githubRepo = SchemaNode(String(), preparer=PREPARERS)
