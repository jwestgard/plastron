import csv
import logging
import re
from collections import defaultdict
from importlib import import_module
from operator import attrgetter
from rdflib import URIRef, Graph


logger = logging.getLogger(__name__)


def configure_cli(subparsers):
    parser = subparsers.add_parser(
        name='import',
        description='Import data to the repository'
    )
    parser.add_argument(
        '-m', '--model',
        help='data model to use',
        action='store'
    )
    parser.add_argument(
        '-l', '--limit',
        help='limit the number of rows to read from the import file',
        type=int,
        action='store'
    )
    parser.add_argument(
        'filename', nargs=1,
        help='name of the file to import from'
    )
    parser.set_defaults(cmd_name='import')


def build_lookup_index(item, index_string):
    # build a lookup index for embedded object properties
    index = defaultdict(dict)
    pattern = r'([\w]+)\[(\d+)\]'
    for entry in index_string.split(';'):
        key, uriref = entry.split('=')
        m = re.search(pattern, key)
        attr = m[1]
        i = int(m[2])
        index[attr][i] = getattr(item, attr)[URIRef(item.uri + uriref)]
    return index


def build_sparql_update(delete_graph, insert_graph):
    deletes = delete_graph.serialize(format='nt').decode('utf-8').strip()
    inserts = insert_graph.serialize(format='nt').decode('utf-8').strip()
    sparql_update = f"DELETE {{ {deletes} }} INSERT {{ {inserts} }} WHERE {{}}"
    return sparql_update


class Command:
    def __call__(self, repo, args):
        module_name, class_name = args.model.rsplit('.', 2)
        model_class = getattr(import_module('plastron.models.' + module_name), class_name)

        property_attrs = {header: attrs for attrs, header in model_class.HEADER_MAP.items()}

        with open(args.filename[0]) as file:
            csv_file = csv.DictReader(file)
            row_count = 0
            updated_count = 0
            unchanged_count = 0
            for row_number, row in enumerate(csv_file, 1):
                if args.limit is not None and row_number > args.limit:
                    logger.info(f'Stopping after {args.limit} rows')
                    break
                logger.debug(f'Processing {args.filename[0]}:{row_number + 1}')
                uri = URIRef(row['URI'])

                # read the object from the repo
                item = model_class.from_graph(repo.get_graph(uri, False), uri)

                index = build_lookup_index(item, row['INDEX'])

                delete_graph = Graph()
                insert_graph = Graph()
                for header, attrs in property_attrs.items():
                    prop = attrgetter(attrs)(item)
                    old_values = [str(v) for v in prop.values]
                    new_values = [v for v in row[header].split('|') if len(v.strip()) > 0]

                    # construct a SPARQL update by diffing for deletions and insertions
                    if '.' not in attrs:
                        subject = uri
                        # simple, non-embedded values

                        # take the set differences to find deleted and inserted values
                        old_values_set = set(old_values)
                        new_values_set = set(new_values)
                        for deleted_value in old_values_set - new_values_set:
                            delete_graph.add((subject, prop.uri, prop.get_term(deleted_value)))
                        for inserted_value in new_values_set - old_values_set:
                            insert_graph.add((subject, prop.uri, prop.get_term(inserted_value)))

                        # and update in-memory, too
                        prop.values = new_values

                    else:
                        # complex, embedded values
                        # if the first portion of the dotted attr notation is a key in the index,
                        # then this column has a different subject than the main uri
                        # correlate positions and urirefs
                        # XXX: for now, assuming only 2 levels of chaining
                        first_attr, next_attr = attrs.split('.', 2)
                        if first_attr in index:
                            for i, new_value in enumerate(new_values):
                                # get the embedded object
                                obj = index[first_attr][i]
                                # TODO: deal with additional new values that don't correspond to old
                                old_value = str(getattr(obj, next_attr).values[0])
                                if new_value != old_value:
                                    setattr(obj, next_attr, new_value)
                                    delete_graph.add((obj.uri, prop.uri, prop.get_term(old_value)))
                                    insert_graph.add((obj.uri, prop.uri, prop.get_term(new_value)))

                # do a pass to remove statements that are both deleted and then re-inserted
                for statement in delete_graph:
                    if statement in insert_graph:
                        delete_graph.remove(statement)
                        insert_graph.remove(statement)

                row_count += 1
                # construct the SPARQL Update query if there are any deletions or insertions
                if len(delete_graph) > 0 or len(insert_graph) > 0:
                    logger.info(f'Sending update for {item}')
                    sparql_update = build_sparql_update(delete_graph, insert_graph)
                    logger.debug(sparql_update)
                    item.patch(repo, sparql_update)
                    updated_count += 1
                else:
                    unchanged_count += 1
                    logger.info(f'No changes found for "{item}" ({uri})')

                # TODO: emit status info

            logger.info(f'{unchanged_count} of {row_count} items remained unchanged')
            logger.info(f'Updated {updated_count} of {row_count} items')