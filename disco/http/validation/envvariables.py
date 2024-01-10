from colander import Schema, SchemaNode, SequenceSchema, String

from disco.http.validation.preparers import PREPARERS

# TODO proper validation


class EnvVariableSchema(Schema):
    name = SchemaNode(String(), preparer=PREPARERS)
    value = SchemaNode(String(), preparer=PREPARERS)


class EnvVariableSequence(SequenceSchema):
    variable = EnvVariableSchema()


class SetEnvVariablesSchema(Schema):
    envVariables = EnvVariableSequence()
