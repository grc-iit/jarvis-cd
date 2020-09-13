from jarvis_cd.argument_parser import ArgumentParser
import sys

if __name__ == '__main__':
    arguments = ArgumentParser.get_instance()
    target = "repos.{}.package".format(arguments.args.target)
    module_ = __import__(target)
    target_ = getattr(module_, arguments.args.target)
    target_ = getattr(target_, "package")
    class_ =  getattr(target_, arguments.args.target.capitalize())
    instance = class_()
    operation = getattr(class_, str(arguments.args.operation).capitalize())
    result = operation(instance)
    print(result)