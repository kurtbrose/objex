
def find_in_tuple_or_list(obj, container):
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
