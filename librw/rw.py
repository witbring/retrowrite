import argparse
from collections import defaultdict

from capstone import CS_OP_IMM, CS_GRP_JUMP, CS_GRP_CALL, CS_OP_MEM
from capstone.x86_const import X86_REG_RIP

from elftools.elf.descriptions import describe_reloc_type
from elftools.elf.enums import ENUM_RELOC_TYPE_x64
from elftools.elf.constants import SH_FLAGS

from .container import Address


class Rewriter():
    GCC_FUNCTIONS = [
        "_start",
        "__libc_start_main",
        "__libc_csu_fini",
        "__libc_csu_init",
        "__lib_csu_fini",
        "_init",
        "__libc_init_first",
        "_fini",
        "_rtld_fini",
        "_exit",
        "__get_pc_think_bx",
        "__do_global_dtors_aux",
        "__gmon_start",
        "frame_dummy",
        "__do_global_ctors_aux",
        "__register_frame_info",
        "deregister_tm_clones",
        "register_tm_clones",
        "__do_global_dtors_aux",
        "__frame_dummy_init_array_entry",
        "__init_array_start",
        "__do_global_dtors_aux_fini_array_entry",
        "__init_array_end",
        "__stack_chk_fail",
        "__cxa_atexit",
        "__cxa_finalize",
    ]

    def __init__(self, container, outfile):
        self.container = container
        self.outfile = outfile

        # Load data sections
        for sec, section in self.container.sections.items():
            section.load()

        # Disassemble all functions
        for function in container.iter_functions():
            if function.name in Rewriter.GCC_FUNCTIONS:
                continue
            # print('Disassembling %s' % function.name)
            function.disasm()

    def symbolize(self):
        symb = Symbolizer()
        symb.symbolize_code_sections(self.container, None)
        symb.symbolize_data_sections(self.container, None)

    def dump(self):
        results = list()

        # Emit rewritten functions
        for function in self.container.iter_functions():
            if function.name in Rewriter.GCC_FUNCTIONS:
                continue
            results.append('.section %s,"ax",@progbits' % function.address.section.name)
            results.append(".align 16")

            # for _, function in sorted(section_functions.items()):
            results.append("%s" % function)

        # Emit rewritten data sections
        for sec, section in sorted(
                self.container.sections.items(), key=lambda x: x[1].base):
            results.append("%s" % (section))

        # Write the final output
        with open(self.outfile, 'w') as outfd:
            outfd.write("\n".join(results + ['']))


class Symbolizer():
    RELOCATION_SIZES = {
        ENUM_RELOC_TYPE_x64['R_X86_64_64']: 8,
        ENUM_RELOC_TYPE_x64['R_X86_64_GOT32']: 4,
        ENUM_RELOC_TYPE_x64['R_X86_64_32']: 4,
        ENUM_RELOC_TYPE_x64['R_X86_64_32S']: 4,
        ENUM_RELOC_TYPE_x64['R_X86_64_16']: 2,
        ENUM_RELOC_TYPE_x64['R_X86_64_8']: 2,
        ENUM_RELOC_TYPE_x64['R_X86_64_PC64']: 8,
        ENUM_RELOC_TYPE_x64['R_X86_64_PC32']: 4,
        ENUM_RELOC_TYPE_x64['R_X86_64_PLT32']: 4,
        ENUM_RELOC_TYPE_x64['R_X86_64_PC16']: 2,
        ENUM_RELOC_TYPE_x64['R_X86_64_PC8']: 1,
        ENUM_RELOC_TYPE_x64['R_X86_64_JUMP_SLOT']: 8,
    }

    def __init__(self):
        self.bases = set()
        self.pot_sw_bases = defaultdict(set)
        self.symbolized_imm = set()
        self.symbolized_mem = set()

    def apply_mem_op_symbolization(self, instruction, target, relocation=None):
        op_mem, _ = instruction.get_mem_access_op()
        assert op_mem

        if op_mem.segment != 0:
            # Segment offsets don't use this form, instead they are like %gs:offset
            instruction.op_str = instruction.op_str.replace(':{}'.format(op_mem.disp), ':' + target.split('+')[0].strip())
        elif op_mem.base == 0 and op_mem.index == 0:
            # Absolute call ds:offset, or in at&t callq *addr. Yes this is a thing in the kernel
            # if instruction.mnemonic == 'movq' and instruction.op_str == '$0, 0(, %rax, 8)':
            if relocation:
                replacement = '*({} - {})'.format(target,
                    Symbolizer.RELOCATION_SIZES[relocation['type']])
            else:
                replacement = '*{}'.format(target)

            instruction.op_str = instruction.op_str.replace(
                '*{}'.format(op_mem.disp),
                replacement
            )
        else:
            instruction.op_str = instruction.op_str.replace('{}('.format(op_mem.disp), str(target) + '(')

        self.symbolized_mem.add(instruction.address)

    def apply_code_relocation(self, instruction, relocation):
        if relocation['symbol_address'] is None:
            # This relocation refers to an imported symbol
            relocation_target = '{} + {}'.format(
                relocation['name'], relocation['addend'] +
                Symbolizer.RELOCATION_SIZES[relocation['type']]
            )
        else:
            if (relocation['type'] in [
                ENUM_RELOC_TYPE_x64['R_X86_64_64'],
                ENUM_RELOC_TYPE_x64['R_X86_64_GOT32'],
                ENUM_RELOC_TYPE_x64['R_X86_64_32'],
                ENUM_RELOC_TYPE_x64['R_X86_64_32S'],
                ENUM_RELOC_TYPE_x64['R_X86_64_16'],
                ENUM_RELOC_TYPE_x64['R_X86_64_8'],
             ]):
                section_offset = relocation['symbol_address'].offset + relocation['addend']
            elif (relocation['type'] in [
                ENUM_RELOC_TYPE_x64['R_X86_64_PC64'],
                ENUM_RELOC_TYPE_x64['R_X86_64_PC32'],
                ENUM_RELOC_TYPE_x64['R_X86_64_PLT32'],
                ENUM_RELOC_TYPE_x64['R_X86_64_PC16'],
                ENUM_RELOC_TYPE_x64['R_X86_64_PC8'],
            ]):
                section_offset = relocation['symbol_address'].offset + relocation['addend'] + \
                    instruction.address.offset + instruction.sz - relocation['address'].offset
            else:
                assert False, 'Unknown relocation type'

            # The target symbol is in this binary
            relocation_target = '.LC{}{:x}'.format(relocation['symbol_address'].section.name,
                section_offset)

        rel_offset_inside_instruction = relocation['address'].offset - instruction.address.offset

        op_imm, op_imm_idx = instruction.get_imm_op()
        op_mem, op_mem_idx = instruction.get_mem_access_op()
        is_jmp = CS_GRP_JUMP in instruction.cs.groups
        is_call = CS_GRP_CALL in instruction.cs.groups

        # We cannot just replace the value of the field with the target (e.g.
        # '0' -> .LC.text.0) because what happens if we have movq $0, 0(%rdi)?
        # both would be replaced which is wrong
        if op_imm is not None and rel_offset_inside_instruction == instruction.cs.imm_offset:
            # Relocation writes to immediate
            if is_jmp or is_call:
                # Direct branch targets are not prefixed with $
                instruction.op_str = relocation_target
            else:
                instruction.op_str = instruction.op_str.replace('${}'.format(op_imm), '$' + relocation_target.split('+')[0].strip())

            self.symbolized_imm.add(instruction.address)
        elif op_mem is not None and rel_offset_inside_instruction == instruction.cs.disp_offset:
            # Relocation writes to displacement
            self.apply_mem_op_symbolization(instruction, relocation_target, relocation)
        else:
            assert False, "Relocation doesn't write to disp or imm"


    # symbolize_code_sections symbolizes all code and data references located in
    # the code sections.
    # There are 4 categories of references that need to be symbolized:
    #   1 - anything that uses relocations. In x86_64 PIE usermode binaries 
    #       these are used for imports (got entries) and init_array. In kernel
    #       modules these are used for anything that references a different
    #       section or a symbol in the main kernel binary or another module.
    #
    #   2 - Direct calls and jumps. These all use an offset relative to the
    #       next instruction, and don't use RIP-relative addressing
    #
    #   3 - RIP-relative data references. There can be no direct data references
    #       because the executable is position-indepent. Indirect jumps and
    #       calls can also have data references
    #
    #   
    def symbolize_code_sections(self, container, context):
        # Symbolize relocations
        for section in container.loader.elffile.iter_sections():
            # Only look for functions in sections that contain code
            if (section['sh_flags'] & SH_FLAGS.SHF_EXECINSTR) == 0:
                continue

            for rel in container.code_relocations[section.name]:
                target_address = rel['address']

                fn = container.function_of_address(target_address)
                if not fn or fn.name in Rewriter.GCC_FUNCTIONS:
                    # Relocation doesn't point into a function
                    continue

                inst = fn.instruction_of_address(target_address)
                if not inst:
                    # Relocation doesn't point to an instruction
                    continue

                self.apply_code_relocation(inst, rel)

        # Symbolize direct branches
        self.symbolize_direct_branches(container, context)

        # Symbolize memory accesses
        self.symbolize_mem_accesses(container, context)
        # self.symbolize_switch_tables(container, context)

    # Symbolize direct branches
    def symbolize_direct_branches(self, container, context=None):
        for function in container.iter_functions():
            for _, instruction in enumerate(function.cache):
                is_jmp = CS_GRP_JUMP in instruction.cs.groups
                is_call = CS_GRP_CALL in instruction.cs.groups

                # Ignore jumps and calls
                if not (is_jmp or is_call):
                    continue

                imm_op = instruction.get_imm_op()[0]
                # Ignore indirect jumps and calls
                if not imm_op:
                    continue

                # Ignore targets that were already symbolized with relocations
                if instruction.address in self.symbolized_imm:
                    continue

                # Capstone should have already computed the right address 
                # (in terms of offset from the start of the section)
                target = container.adjust_address(Address(instruction.address.section, imm_op))
                instruction.op_str = '.LC%s' % str(target)


    def symbolize_switch_tables(self, container, context):
        rodata = container.sections.get(".rodata", None)
        if not rodata:
            return

        all_bases = set([x for _, y in self.pot_sw_bases.items() for x in y])

        for faddr, swbases in self.pot_sw_bases.items():
            fn = container.function_of_address(faddr)
            for swbase in sorted(swbases, reverse=True):
                value = rodata.read_at(swbase, 4)
                if not value:
                    continue

                value = (value + swbase) & 0xffffffff
                if not fn.is_valid_instruction(value):
                    continue

                # We have a valid switch base now.
                swlbl = ".LC%x-.LC%x" % (value, swbase)
                rodata.replace(swbase, 4, swlbl)

                # Symbolize as long as we can
                for slot in range(swbase + 4, rodata.base + rodata.sz, 4):
                    if any([x in all_bases for x in range(slot, slot + 4)]):
                        break

                    value = rodata.read_at(slot, 4)
                    if not value:
                        break

                    value = (value + swbase) & 0xFFFFFFFF
                    if not fn.is_valid_instruction(value):
                        break

                    swlbl = ".LC%x-.LC%x" % (value, swbase)
                    rodata.replace(slot, 4, swlbl)

    # Symbolize memory accesses
    def symbolize_mem_accesses(self, container, context):
        for function in container.iter_functions():
            for instruction in function.cache:
                mem_access, _ = instruction.get_mem_access_op()

                # Ignore instructions that don't access memory
                if not mem_access:
                    continue

                # Ignore non-RIP relative references
                if mem_access.base != X86_REG_RIP:
                    continue

                if instruction.address in self.symbolized_mem:
                    continue

                target = container.adjust_address(
                    Address(instruction.address.section,
                        instruction.address.offset + instruction.sz +
                        mem_access.disp
                    )
                )

                self.apply_mem_op_symbolization(instruction, target)



    def _handle_relocation(self, container, section, relocation):
        reloc_type = relocation['type']
        if reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_COPY"]:
            # NOP
            return

        relocation_size = Symbolizer.RELOCATION_SIZES[relocation['type']]
        relocation_target = None

        if relocation['symbol_address'] is None:
            # This relocation refers to an imported symbol
            relocation_target = '{} + {}'.format(relocation['name'], relocation['addend'])

        if reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_PC32"]:
            if not relocation_target:
                value = relocation['symbol_address'].offset + relocation['addend']
                relocation_target = '.LC%s%x' % (relocation['symbol_address'].section.name, value)
            relocation_target += ' - .'
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_PC64"]:
            if not relocation_target:
                value = relocation['symbol_address'].offset + relocation['addend']
                relocation_target = '.LC%s%x' % (relocation['symbol_address'].section.name, value)
            relocation_target += ' - .'
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_32S"]:
            if not relocation_target:
                value = relocation['symbol_address'].offset + relocation['addend']
                relocation_target = '.LC%s%x' % (relocation['symbol_address'].section.name, value)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_64"]:
            if not relocation_target:
                value = relocation['symbol_address'].offset + relocation['addend']
                relocation_target = '.LC%s%x' % (relocation['symbol_address'].section.name, value)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_RELATIVE"]:
            if not relocation_target:
                value = relocation['addend']
                relocation_target = '.LC%s%x' % (relocation['symbol_address'].section.name, value)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_JUMP_SLOT"]:
            if not relocation_target:
                value = relocation['symbol_address'].offset
                relocation_target = '.LC%s%x' % (relocation['symbol_address'].section.name, value)
        else:
            print("[*] Unhandled relocation {}".format(
                describe_reloc_type(reloc_type, container.loader.elffile)))

        if relocation_size:
            section.replace(relocation['address'].offset, relocation_size, relocation_target)

    def symbolize_data_sections(self, container, context=None):
        # Section specific relocation
        for secname, section in container.sections.items():
            for relocation in section.relocations:
                self._handle_relocation(container, section, relocation)


def is_data_section(sname, sval, container):
    # A data section should be present in memory (SHF_ALLOC), and its size should
    # be greater than 0. There are some code sections in kernel modules that
    # only contain short trampolines and don't have any function relocations
    # in them. The easiest way to deal with them for now is to treat them as
    # data sections but this is a bit of a hack because they could contain
    # references that need to be symbolized
    return (
        (sval['flags'] & SH_FLAGS.SHF_ALLOC) != 0 and (
            (sval['flags'] & SH_FLAGS.SHF_EXECINSTR) == 0 or sname not in container.code_section_names
        ) and sval['sz'] > 0
    )


def is_readonly_data_section(section):
    return (
        (section['sh_flags'] & SH_FLAGS.SHF_ALLOC) != 0 and
        (section['sh_flags'] & SH_FLAGS.SHF_EXECINSTR) == 0 and
        (section['sh_flags'] & SH_FLAGS.SHF_WRITE) == 0
    )


if __name__ == "__main__":
    from .loader import Loader
    from .analysis import register

    argp = argparse.ArgumentParser()

    argp.add_argument("bin", type=str, help="Input binary to load")
    argp.add_argument("outfile", type=str, help="Symbolized ASM output")

    args = argp.parse_args()

    loader = Loader(args.bin)

    flist = loader.flist_from_symtab()
    loader.load_functions(flist)

    slist = loader.slist_from_symtab()
    loader.load_data_sections(slist, is_data_section)

    reloc_list = loader.reloc_list_from_symtab()
    loader.load_relocations(reloc_list)

    global_list = loader.global_data_list_from_symtab()
    loader.load_globals_from_glist(global_list)

    loader.container.attach_loader(loader)

    rw = Rewriter(loader.container, args.outfile)
    rw.symbolize()
    rw.dump()
