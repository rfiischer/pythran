import ast
from cgen import *

from passes import local_declarations, global_declarations, remove_comprehension, constant_value, remove_nested_functions, remove_lambdas
from tables import operator_to_lambda, modules
from typing import type_all

templatize = lambda node, types: Template([ "typename " + t for t in types ], node ) if types else node 

class PythranSyntaxError(SyntaxError):
    def __init__(self, msg, node):
        SyntaxError.__init__(self,msg)
        self.lineno=node.lineno
        self.offset=node.col_offset


class CgenVisitor(ast.NodeVisitor):
    def __init__(self, name):
        self.name=name
        self.types=None
        self.declarations=list()
        self.definitions=list()

    # mod
    def visit_Module(self, node):
        # sanitize input
        remove_comprehension(node)
        remove_nested_functions(node)
        remove_lambdas(node)
        # build all types
        self.global_declarations = global_declarations(node)
        self.local_functions=set()
        self.local_declarations=list()
        self.types=type_all(node)
        headers= [ Include(h) for h in [ "pythran/pythran.h", "boost/python/module.hpp" ] ]
        body = [ self.visit(n) for n in node.body ]

        assert not self.local_declarations
        return headers +  [Namespace(self.name, body + self.declarations + self.definitions) ]

    def visit_Interactive(self, node):
        raise PythranSyntaxError("Interactive session are not supported", node)

    def visit_Expression(self, node):
        raise PythranSyntaxError("Top-Level expressions are not supported", node)

    def visit_Suite(self, node):
        raise PythranSyntaxError("Suite are specific to Jython and not supported", node)

    # stmt
    def visit_FunctionDef(self, node):
        fargs = node.args.args

        local_functions={k for k in self.local_functions}

        formal_args = [ arg.id for arg in fargs ]
        formal_types= [ "argument_type"+str(i) for i in xrange(len(formal_args)) ]

        ldecls={ sym.id:sym for sym in local_declarations(node) }

        self.local_declarations.append(set(ldecls.iterkeys()))
        self.local_declarations[-1].update(formal_args)

        ldecls= set(ldecls.itervalues())

        operator_body = [ self.visit(n) for n in node.body ] 

        self.local_declarations.pop()

        return_declaration = [templatize(Struct("type",[Typedef(Value(self.types[node], "return_type"))]), formal_types)]

        fscope = "type{0}::".format( "<{0}>".format(", ".join(formal_types)) if formal_types else ""  )
        operator_declaration = [templatize(FunctionDeclaration( Value("typename "+fscope+"return_type", "operator()"),
            [ Value( t, a ) for t,a in zip(formal_types, formal_args) ] ), formal_types), EmptyStatement()] #*** empty statement to force a comma ...

        topstruct = Struct(node.name, return_declaration + operator_declaration)
        self.declarations.append(topstruct)

        ffscope = "{0}::{1}".format(node.name, fscope)
        operator_signature = FunctionDeclaration(
                Value("typename {0}return_type".format(ffscope), "{0}::operator()".format(node.name)),
                [ Value( t, a ) for t,a in zip(formal_types, formal_args ) ] )
        operator_local_declarations = [ Statement("{0} {1}".format(self.types[k], k.id)) for k in ldecls if "/*auto*/" not in self.types[k]]
        operator_definition = FunctionBody(
                templatize(operator_signature, formal_types),
                Block( operator_local_declarations + operator_body )
                )

        self.definitions.append(operator_definition)
        self.local_functions=local_functions
        return EmptyStatement()

    def visit_ClassDef(self, node):
        raise PythranSyntaxError("Classes not supported")

    def visit_Return(self, node):
        return ReturnStatement(self.visit(node.value) if node.value else "")

    def visit_Delete(self, node):
        return EmptyStatement()

    def visit_Assign(self, node):
        if not all([isinstance(n, ast.Name) or isinstance(n, ast.Subscript) for n in node.targets]) :
            raise PythranSyntaxError("Assigning to something other than an identifier or a subscript", node)
        value = self.visit(node.value)
        targets=[self.visit(t) for t in node.targets]
        alltargets="= ".join(targets)
        if "/*auto*/" in self.types[node.value]: return Statement("auto {0} = {1}".format(alltargets, value))
        else: return Statement("{0} = {1}".format(alltargets, value))

    def visit_AugAssign(self, node):
        if not isinstance(node.target, ast.Name):
            raise PythranSyntaxError("Assigning to something other than an identifier", node)
        value = self.visit(node.value)
        target=self.visit(node.target)
        return Statement(operator_to_lambda[type(node.op)](target, "="+value))

    def visit_Print(self, node):
        if node.dest: raise PythranSyntaxError("Printing to a specific stream", node.dest)
        values = [ self.visit(n) for n in node.values]
        return Statement("print{0}({1})".format(
                "" if node.nl else "_nonl",
                ", ".join(values))
                )

    def visit_For(self, node):
        if not isinstance(node.target, ast.Name):
            raise PythranSyntaxError("Using something other than an identifier as loop target", node.target)
        iter = self.visit(node.iter)
        target = self.visit(node.target)
        body = [ self.visit(n) for n in node.body ]
        orelse = [ self.visit(n) for n in node.orelse ]
        stmt = AutoFor(target, iter, Block(body))
        return If(iter, stmt, Block(orelse)) if orelse else stmt

    def visit_While(self, node):
        test = self.visit(node.test)
        body = [ self.visit(n) for n in node.body ]
        orelse = [ self.visit(n) for n in node.orelse ]
        stmt = While(test, Block(body))
        return If(test, Block( body + [stmt]), Block(orelse)) if orelse else stmt 

    def visit_If(self, node):
        test = self.visit(node.test)
        body = [ self.visit(n) for n in node.body ]
        orelse = [ self.visit(n) for n in node.orelse ]
        return If(test, Block(body), Block(orelse) if orelse else None )

    def visit_Assert(self, node):
        params = [ self.visit(node.msg) if node.msg else None, self.visit(node.test) ]
        return Statement("assert(({0}))".format(", ".join(p for p in params if p)))

    def visit_ImportFrom(self, node):
        if node.level != 0: raise PythranSyntaxError("Specifying a level in an import", node)
        if not node.module: raise PythranSyntaxError("The import from syntax without module", node)
        module = node.module
        if module not in modules: raise PythranSyntaxError("Module '{0}'".format(module), node)

        names = node.names
        if [ alias for alias in names if alias.asname ]: raise PythranSyntaxError("Renaming using the 'as' keyword in an import", node)

        usings=list()
        for alias in names:
            if modules[node.module][alias.name]:
                usings.append("using {0}::{1}".format(module, alias.name))
            else:
                self.local_functions.add(alias.name)
                usings.append("using proxy::{0}".format(alias.name))

        return Statement("; ".join(usings))

    def visit_Expr(self, node):
        return Statement(self.visit(node.value))

    def visit_Pass(self, node):
        return EmptyStatement()

    def visit_Break(self, node):
        return Statement("break")

    def visit_Continue(self, node):
        return Statement("continue")


    # expr
    def visit_BoolOp(self, node):
        values = [ self.visit(value) for value in node.values ]
        return reduce(operator_to_lambda[type(node.op)], values)

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right= self.visit(node.right)
        return operator_to_lambda[type(node.op)](left,right)

    def visit_UnaryOp(self, node):
        operand = self.visit(node.operand)
        return operator_to_lambda[type(node.op)](operand)

    def visit_IfExp(self, node):
        test = self.visit(node.test)
        body = self.visit(node.body)
        orelse = self.visit(node.orelse)
        return "({0} ? {1} : {2})".format(test, body, orelse)

    def visit_List(self, node):
        if not node.elts: # empty list
            return "list()"
        else:
            elts = [ self.visit(n) for n in node.elts ]
            return "sequence< decltype({0})>({{ {1} }})".format(" + ".join(elts), ", ".join(elts))

    def visit_Tuple(self, node):
        if not node.elts: # empty tuple
            return "std::make_tuple()"
        else:
            elts = [ self.visit(n) for n in node.elts ]
            return "std::make_tuple({0})".format(", ".join(elts))


    def visit_Compare(self, node):
        left = self.visit(node.left)
        ops = [ operator_to_lambda[type(n)] for n in node.ops ]
        comparators = [ self.visit(n) for n in node.comparators ]
        all_compare = zip( [left]+comparators[:-1], ops, comparators )
        return " and ".join(op(l,r) for l,op,r in all_compare)

    def visit_Call(self, node):
        args = [ self.visit(n) for n in node.args ]
        func = self.visit(node.func)
        return "{0}({1})".format(func, ", ".join(args))

    def visit_Num(self, node):
        return str(node.n)

    def visit_Str(self, node):
        return '"{0}"'.format(node.s)

    def visit_Subscript(self, node):
        value = self.visit(node.value)
        slice = self.visit(node.slice)
        try:
            v = constant_value(node.slice)
            return "std::get<{0}>({1})".format(v, value)
        except:
            return "{1}[{0}]".format(slice, value)

    def visit_Name(self, node):
        if node.id in self.local_declarations[-1]:
            return node.id
        elif node.id in modules["__builtins__"]:
            return "proxy::{0}()".format(node.id)
        elif node.id in self.global_declarations or node.id in self.local_functions:
            return "{0}()".format(node.id)
        else:
            return node.id


    # other

    def visit_Slice(self, node):
        lower = self.visit(node.lower) if node.lower else None
        upper = self.visit(node.upper) if node.upper else None
        step = self.visit(node.step) if node.step else None
        if not upper and not lower and step: # special case
            raise NotImplementedError
        if step:
            if not upper: upper = "std::numeric_limits<long>::max()"
        if upper:
            if not lower: lower = "0"
        return "slice({0})".format(", ".join( l for l in [ lower, upper, step ] if l ))

    def visit_Index(self, node):
        value = self.visit(node.value)
        return value
