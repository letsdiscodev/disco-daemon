from colander import OneOf, Schema, SchemaNode, String

from disco.http.validation.preparers import PREPARERS


# TODO proper validation
class SyslogUrlSchema(Schema):
    action = SchemaNode(
        String(), preparer=PREPARERS, validators=OneOf(["add", "remove"])
    )
    url = SchemaNode(String(), preparer=PREPARERS)
