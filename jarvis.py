from jarvis_cd.argument_parser import ArgumentParser
import sys
import logging

if __name__ == '__main__':
    arguments = ArgumentParser.get_instance()
    logging.basicConfig(filename=arguments.args.log_path, level=int(arguments.args.log_level.value),filemode='w', format='[%(levelname)s] %(asctime)s :%(message)s')
    logging.debug("Target {} and Operation {}".format(arguments.args.target, arguments.args.operation))
    target = "repos.{}.package".format(arguments.args.target)
    module_ = __import__(target)
    logging.info("Loaded repos")
    target_ = getattr(module_, arguments.args.target)
    logging.info("Loaded {}".format(arguments.args.target))
    target_ = getattr(target_, "package")
    logging.info("Loaded package.py module")
    class_ =  getattr(target_, arguments.args.target.capitalize())
    logging.info("Loaded class {}".format(arguments.args.target.capitalize()))
    instance = class_()
    operation = getattr(class_, str(arguments.args.operation).capitalize())
    logging.info("Loaded operation {}".format(str(arguments.args.operation).capitalize()))
    result = operation(instance)
    logging.info("Finished execution", exc_info=True)
