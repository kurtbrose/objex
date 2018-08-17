
def find_in_tuple_or_list(obj, container):
    '''
    returns iterator over all of the instances of obj in container
    '''
    cdef list list_container
    cdef tuple tuple_container
    cdef int i
    if type(container) is list:
        list_container = <list>container
        for i in range(len(list_container)):
            if list_container[i] is obj:
                yield i
    elif type(container) is tuple:
        tuple_container = <tuple>container
        for i in range(len(tuple_container)):
            if tuple_container[i] is obj:
                yield i
    else:
        for i in range(len(container)):
            if container[i] is obj:
                yield i

def get_keys_dst(obj, list referents):
    '''
    given an object and things that it refers to,
    return [(key, dst), (key, dst), ....]
    '''
    cdef list results
    if type(obj) in (list, tuple):
        return enumerate(obj)
    if type(obj) is dict:
        return get_dict_keys_dst(<dict>obj, referents)
    results = []
    if hasattr(obj, '__dict__'):
        obj.__dict__.keys():
            results.append(('<key>', ))




cdef get_dict_keys_dst(dict obj, list referents):
    for ref in referents:
        for key in obj:
            if ref is key:
                yield ('<key>', ref)
            if obj[key] is ref:
                yield (repr(key), ref)
