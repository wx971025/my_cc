"""
冒泡排序（Bubble Sort）
原理：重复遍历列表，依次比较相邻元素，若顺序错误则交换，
      每轮遍历都会将当前最大值"冒泡"到末尾。
时间复杂度：O(n²)
空间复杂度：O(1)
"""


def bubble_sort(arr):
    """
    冒泡排序函数
    :param arr: 待排序列表
    :return: 排序后的列表（原地排序）
    """
    n = len(arr)

    for i in range(n):
        # 标志位：若本轮没有发生交换，说明已经有序，提前退出
        swapped = False

        # 每轮将最大值冒泡到末尾，已排好的部分不再比较
        for j in range(0, n - i - 1):
            if arr[j] > arr[j + 1]:
                # 交换相邻元素
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
                swapped = True

        # 若本轮没有交换，列表已有序，提前退出
        if not swapped:
            break

    return arr


# ---- 测试示例 ----
if __name__ == "__main__":
    test_data = [64, 34, 25, 12, 22, 11, 90]
    print(f"排序前：{test_data}")
    result = bubble_sort(test_data)
    print(f"排序后：{result}")

    # 测试已有序列表
    sorted_data = [1, 2, 3, 4, 5]
    print(f"\n排序前：{sorted_data}")
    bubble_sort(sorted_data)
    print(f"排序后：{sorted_data}")

    # 测试单元素列表
    single = [42]
    print(f"\n排序前：{single}")
    bubble_sort(single)
    print(f"排序后：{single}")
