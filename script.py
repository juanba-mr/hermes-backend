import xlrd

def inspeccionar_excel(ruta, filas_a_mostrar=[6, 24]):
    """
    Lee un archivo .xls binario y muestra el contenido 
    de filas específicas de forma legible.
    """
    try:
        # Abrimos el libro de excel (formato binario .xls)
        book = xlrd.open_workbook(ruta)
        sheet = book.sheet_by_index(0) # Tomamos la primera hoja

        print(f"--- INSPECCIONANDO: {ruta} ---")
        print(f"Total de filas detectadas: {sheet.nrows}")
        print("-" * 40)

        for n_fila in filas_a_mostrar:
            # Restamos 1 porque xlrd empieza a contar desde 0
            indice = n_fila - 1 
            
            if indice < sheet.nrows:
                # Obtenemos todos los valores de la fila
                valores = sheet.row_values(indice)
                print(f"CONTENIDO LÍNEA {n_fila}:")
                print(valores)
            else:
                print(f"LÍNEA {n_fila}: Fuera de rango (el archivo tiene menos filas).")
            print("-" * 40)

    except Exception as e:
        print(f"No se pudo leer el archivo como Excel: {e}")

if __name__ == "__main__":
    # Cambiá la ruta por la de tu archivo
    ruta_archivo = 'data/RUSSS.xls'
    inspeccionar_excel(ruta_archivo, filas_a_mostrar=[1, 5, 6, 24])